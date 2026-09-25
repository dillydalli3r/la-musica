import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, Save, RotateCcw, LayoutGrid, Settings as SettingsIcon, Check, Eye, EyeOff, ChevronDown, ChevronUp, Wand2, X, FolderOpen, Loader2, Copy, Trash2 } from "lucide-react";
import { api, deviceUnavailable, unavailableFeatures } from "../api";
import ConfirmButton from "../components/ConfirmButton";
import CookieJarPanel from "../components/CookieJarPanel";
import FolderPicker from "../components/FolderPicker";
import SourcesPanel from "../components/SourcesPanel";
import SecurityPanel from "../components/SecurityPanel";
import AiTestButton from "../components/AiTestButton";
import PageHeader from "../components/PageHeader";
import { toast } from "../store";
import { applyAccent } from "../App";
import { ACCENT_PRESETS, DEFAULT_ACCENT, accentHex, normalizeHex, parseHexColor, presetHex, resolveAccent } from "../lib/accent";
import { DEFAULT_RUN_ALL, OPT_IN_SCRIPTS, SCRIPT_LABEL, isScriptId } from "../lib/scripts";
import { LOCALES, applyConfigLocale, setLocale, useI18n, type MessageKey } from "../lib/i18n";
import { CODEC_CHOICES } from "../lib/codecMeta";
import { notificationState, requestNotifications, type NotifyState } from "../lib/notify";
import { pbDiagClear, pbDiagEvents, pbDiagVersion, subscribe, type DiagEvent } from "../lib/pbDiag";
import { iosShellState } from "../lib/iosState";

/** The accent swatches, each with the ink its own colour needs for the tick —
 *  derived (lib/accent), not a hardcoded `text-black`, which was invisible on
 *  the dark swatches. The row itself comes from ACCENT_PRESETS, so a preset
 *  that exists is a preset that is offered. */
const ACCENT_OPTIONS: { id: string; hex: string; fg: string }[] = ACCENT_PRESETS.map((p) => ({
  id: p.id,
  hex: p.hex,
  fg: resolveAccent(p.id)[2],
}));

/** The swatches' names, one translated key per preset id. A `Record` keyed by
 *  id rather than a template string, so `t()` is type-checked here: a preset
 *  whose name was never added to the bundles fails the build instead of
 *  showing its raw key in the tooltip. */
const ACCENT_LABEL: Record<string, MessageKey> = {
  red: "settings.accent_red",
  orange: "settings.accent_orange",
  amber: "settings.accent_amber",
  yellow: "settings.accent_yellow",
  lime: "settings.accent_lime",
  emerald: "settings.accent_emerald",
  teal: "settings.accent_teal",
  sky: "settings.accent_sky",
  blue: "settings.accent_blue",
  indigo: "settings.accent_indigo",
  violet: "settings.accent_violet",
  fuchsia: "settings.accent_fuchsia",
  pink: "settings.accent_pink",
  rose: "settings.accent_rose",
  mono: "settings.accent_mono",
};

const ENCODER_FORMATS = ["flac", "jpeg", "png", "jxl"] as const;
const ENCODER_FIELDS = ["ENCODER_PROGRAM", "ENCODER_QUALITY", "ENCODER_VERSION"] as const;

/** Interface zoom, as a percentage of the app's normal size.
 *
 *  Stored per browser (like the accent color and the player's own picks), and
 *  APPLIED by the app shell: web/src/App.tsx reads this key and scales the
 *  root, so this page only writes the value and tells it (the shell listens
 *  for `mlo:zoom`). 80–150%: below that the player's own controls stop being
 * tappable on a phone, above it a desktop window shows two albums and one
 *  album's worth of scrolling. */
const ZOOM_KEY = "mlo.zoom";
const ZOOM_DEFAULT = 100;

function readZoom(): number {
  const saved = Number(localStorage.getItem(ZOOM_KEY));
  return saved >= 80 && saved <= 150 ? saved : ZOOM_DEFAULT;
}
const AUDIO_TYPES = ["flac", "mp3", "mp4", "ogg", "opus", "aac"] as const;
const TAG_FAMILIES = ["AUDIT", "LOG_GRADE", "REPLAYGAIN", "DYNAMIC_RANGE", "MEDIA_SOURCE", "INSTRUMENTAL", "ADVISORY", "LYRICS", "GENRE", "BPM", "INITIALKEY", "MOOD", "ENERGY"] as const;

/** Wave-2 keys an older config file predates. The form reads them through
 *  this map so a fresh install shows the real default (the backend fills in
 *  the same values when it loads the file) instead of an empty field. */
const CFG_DEFAULTS: Record<string, unknown> = {
  mb_genre_count: 2,
  genre_sources: ["rateyourmusic", "musicbrainz", "listenbrainz", "itunes", "lastfm", "theaudiodb", "wikidata", "bandcamp", "discogs", "deezer", "spotify"],
  advisory_auto_fetch: true,
  metadata_auto_fetch: true,
  metadata_review: false,
  soulseek_download_slots: 9,
  soulseek_candidate_slots: 3,
  soulseek_upload_slots: 2,
  soulseek_upload_limit_kib: 0,
  soulseek_download_limit_kib: 0,
  soulseek_web_https: false,
  soulseek_cache_cap_gb: 5,
  trash_cap_gb: 5,
  youtube_enabled: true,
  youtube_max_height: 0,
  auto_import_avoid_promo: true,
  auto_import_require_country: true,
  prefer_release_country: "",
  prefer_original_edition: true,
  prefer_disc_streams: true,
  auto_import_medium_order: ["CD", "Vinyl", "Cassette", "Other", "DVD",
    "Blu-ray", "VHS", "Video CD", "LaserDisc", "Digital Media"],
  cover_auto_fetch: true,
  cover_review: false,
};

type ProviderOption = { id: string; label: string; notes?: string; rank?: number };

/** Genre sources that never answer for a single track — the chain files their
 *  answer under every track and marks it `level: "album"`/`"artist"`
 *  (server/integrations._genre_source_answers). Everything absent here is
 *  asked per track first, with its own album/artist answer as the fallback
 *  tier, so "per track" is what the picker says for them. */
const GENRE_LEVEL: Record<string, string> = {
  rateyourmusic: "album, per track when its release page states one",
  discogs: "album only",
  bandcamp: "album only",
  deezer: "album only",
  spotify: "artist only",
};

/** Ordered provider picker for a `list` config key: the listed providers are
 *  tried in order (first one with an answer wins). An empty list means the
 *  built-in order, shown as a placeholder. */
function ProviderOrder({
  options, builtin, order, onChange, help,
}: {
  options: ProviderOption[];
  builtin: string[];
  order: string[];
  onChange: (next: string[]) => void;
  help?: string;
}) {
  const move = (i: number, d: number) => {
    const next = order.slice();
    [next[i], next[i + d]] = [next[i + d], next[i]];
    onChange(next);
  };
  return (
    <div className="space-y-1.5">
      {order.length === 0 ? (
        <div className="rounded-md border border-dashed border-border px-2 py-1.5 text-[11px] text-zinc-500">
          <span className="text-zinc-400">Built-in order: </span>
          {builtin.map((id) => options.find((o) => o.id === id)?.label ?? id).join(" → ")}
        </div>
      ) : (
        <div className="rounded-md border border-border divide-y divide-border/60">
          {order.map((id, i) => {
            const opt = options.find((o) => o.id === id);
            return (
              <div key={id} className="flex items-start gap-2 px-2 py-1">
                <span className="w-4 pt-0.5 text-[10px] text-zinc-600">{i + 1}</span>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[12px] text-zinc-200 truncate">{opt?.label ?? id}</span>
                    {opt?.rank !== undefined && (
                      <span
                        className="chip border border-accent/25 bg-accent/10 text-accent-soft shrink-0"
                        title="Rank in the BUILT-IN chain — this list replaces the order, it does not change the rank"
                      >
                        #{opt.rank} preferred
                      </span>
                    )}
                  </div>
                  {opt?.notes && <div className="text-[10px] text-zinc-600">{opt.notes}</div>}
                </div>
                <button
                  type="button"
                  className="min-h-8 min-w-8 md:min-h-0 md:min-w-0 flex items-center justify-center text-zinc-500 hover:text-white disabled:opacity-30"
                  title="Move up"
                  disabled={i === 0}
                  onClick={() => move(i, -1)}
                >
                  <ChevronUp className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  className="min-h-8 min-w-8 md:min-h-0 md:min-w-0 flex items-center justify-center text-zinc-500 hover:text-white disabled:opacity-30"
                  title="Move down"
                  disabled={i === order.length - 1}
                  onClick={() => move(i, 1)}
                >
                  <ChevronDown className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  className="min-h-8 min-w-8 md:min-h-0 md:min-w-0 flex items-center justify-center text-zinc-500 hover:text-red-300"
                  title="Remove from the list (unlisted providers are not used)"
                  onClick={() => onChange(order.filter((x) => x !== id))}
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            );
          })}
        </div>
      )}
      {options.some((o) => !order.includes(o.id)) && (
        <div className="flex flex-wrap gap-1.5">
          {options.filter((o) => !order.includes(o.id)).map((o) => (
            <button
              key={o.id}
              type="button"
              className="chip border border-white/15 bg-white/5 text-[10px] text-zinc-400 hover:text-white tap"
              title={o.notes}
              onClick={() => onChange([...order, o.id])}
            >
              + {o.label}
            </button>
          ))}
        </div>
      )}
      {order.length > 0 && (
        <button
          type="button"
          className="text-[10px] text-zinc-500 hover:text-white underline tap"
          onClick={() => onChange([])}
        >
          Reset to the built-in order
        </button>
      )}
      {help && <div className="text-[10px] text-zinc-600">{help}</div>}
    </div>
  );
}

/** Cover-art defaults (Settings → Images): the region and source list a cover
 *  search starts from. These are the SAVED values (`cover_country`,
 *  `cover_sources`) that a search with no overrides uses — the cover finder's
 *  own region/source pickers are per-search and change nothing here until its
 *  "Save as default" is pressed. */
function CoverDefaults() {
  const qc = useQueryClient();
  const { data: cat } = useQuery({ queryKey: ["coverSources"], queryFn: api.coverSources });
  const [srcSel, setSrcSel] = useState<string[] | null>(null);
  const [country, setCountry] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Seeded from the EFFECTIVE values (`default_sources`/`default_country` are
  // what a search with no overrides uses — the saved list, or the built-in one
  // while nothing is saved), and re-seeded whenever they change so a save made
  // in the finder shows up here instead of leaving a stale draft behind.
  const savedKey = `${cat?.default_country ?? ""}|${(cat?.default_sources ?? []).join(",")}`;
  useEffect(() => {
    if (!cat) return;
    setSrcSel(cat.default_sources);
    setCountry(cat.default_country);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [savedKey]);

  if (!cat || srcSel === null || country === null) {
    return <div className="text-[11px] text-zinc-600">Loading the cover source catalogue…</div>;
  }
  const cap = cat.active_source_limit;
  const dirty = srcSel.join(",") !== cat.default_sources.join(",") || country !== cat.default_country;

  const save = async () => {
    setBusy(true);
    try {
      await api.saveConfig({ cover_sources: srcSel, cover_country: country });
      qc.invalidateQueries({ queryKey: ["coverSources"] });
      qc.invalidateQueries({ queryKey: ["config"] });
      toast.success(`Saved the default cover search: ${srcSel.length} source(s), ${country.toUpperCase()}`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Cover art</div>
      <div className="text-[11px] text-zinc-600">
        The defaults a cover search starts from. The finder's own region and source pickers are
        <span className="text-zinc-400"> per search</span> — they change nothing here until you press
        its "Save as default", which writes exactly these two values.
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] font-semibold text-zinc-400">Region</span>
        <select
          className="input !w-auto !py-1 text-xs tap"
          value={country}
          onChange={(e) => setCountry(e.target.value)}
        >
          {cat.countries.map((c) => (
            <option key={c} value={c}>{c.toUpperCase()}</option>
          ))}
        </select>
        <span className="text-[10px] text-zinc-600">
          The storefront the sources are asked about — it decides which releases and artwork exist for a region.
        </span>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] font-semibold text-zinc-400">Sources</span>
        <span className="text-[10px] text-zinc-500">
          {srcSel.length}/{cap} — at most {cap} are used per search
        </span>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1">
        {cat.sources.map((s) => {
          const on = srcSel.includes(s.id);
          const full = !on && srcSel.length >= cap;
          return (
            <label
              key={s.id}
              className={`flex items-center gap-1.5 text-[11px] select-none ${
                full ? "text-zinc-600" : "text-zinc-300 cursor-pointer"
              }`}
              title={full ? `Already at the ${cap}-source limit` : s.name}
            >
              <input
                type="checkbox"
                checked={on}
                disabled={full}
                onChange={() =>
                  setSrcSel(srcSel.includes(s.id) ? srcSel.filter((x) => x !== s.id) : [...srcSel, s.id])
                }
              />
              {s.color && <span className="h-2 w-2 rounded-full shrink-0" style={{ background: s.color }} />}
              {s.name}
              {!s.enabled && <span className="text-[9px] text-amber-400/80">off</span>}
            </label>
          );
        })}
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <button className="btn-primary !py-1 text-xs min-h-10 md:min-h-0" onClick={save} disabled={busy || !dirty}>
          <Check className="h-3 w-3" /> Save as default
        </button>
        {dirty && (
          <button
            className="btn-ghost !py-1 text-xs tap"
            disabled={busy}
            onClick={() => {
              setSrcSel(cat.default_sources);
              setCountry(cat.default_country);
            }}
          >
            <RotateCcw className="h-3 w-3" /> Discard changes
          </button>
        )}
        {!dirty && <span className="text-[10px] text-zinc-600">Saved — a search with no overrides uses this.</span>}
      </div>
    </div>
  );
}

/** The YouTube cookie jar (Settings → Videos).
 *
 *  The panel itself is `components/CookieJarPanel.tsx` — ONE implementation for
 *  every cookie login, shared with Settings → Sources, the wizard's Keys step
 *  and the Discovery tab's RYM box, so the two credentials' import boxes (and
 *  the per-cookie comment list both now show) cannot drift apart. This wrapper
 *  is only where the Videos tab renders it, exactly as before.
 */
function YoutubeCookieJar() {
  return <CookieJarPanel source="youtube" />;
}

/** The RateYourMusic cookie (Settings → Discovery): the same shared panel for
 *  the RYM credential.
 *
 *  `onStored` pulls the imported value back into the `rym_cookie` field above:
 *  the credential is ONE config value, so after an import the box (and the next
 *  "Save all settings") has to agree with what RYM is actually sent.
 */
function RymCookieJar({ onStored }: { onStored: (value: string) => void }) {
  return <CookieJarPanel source="rym" onStored={onStored} />;
}

/** HH:MM:SS on the reader's own clock. The report is read next to the moment
 *  the owner heard the sound stop, so local time is the only useful clock —
 *  an ISO string with a timezone in it would be one more thing to translate on
 *  a phone screenshot. */
function clockOf(t: number): string {
  const d = new Date(t);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** One event's detail as `key=value …`, in the order the recorder wrote it —
 *  the same text the panel draws and the Copy button puts on the clipboard, so
 *  what is read and what is sent can never disagree. */
function diagDetailText(detail: DiagEvent["detail"]): string {
  if (!detail) return "";
  return Object.entries(detail)
    .map(([k, v]) => `${k}=${v === null ? "null" : v}`)
    .join(" ");
}

/** The on-device playback report (lib/pbDiag) — the black box for the iOS
 *  reports that cannot be reproduced anywhere else: "audio stops when I tab
 *  out", "the lock-screen controls do nothing".
 *
 *  Everything here is built to be READ ON A PHONE and then handed over: the
 *  groups stack, every value sits under its own key, nothing scrolls sideways,
 *  and Copy writes the whole report as tab-separated lines so it survives being
 *  pasted into a chat. Live by subscription: a row the player appends while
 *  this block is open appears here without a refresh (the point is to watch it
 *  happen, on the device that misbehaves).
 *
 *  The shell group is asked for ONCE, when the block is first opened — the IPC
 *  is only reachable inside the Tauri shell (lib/iosState), and a Settings page
 *  nobody opened the report on must not pay for it. No shell answering is a
 *  fact worth stating ("no shell (browser)"), never a spinner. */
function PlaybackDiag() {
  const { t } = useI18n();
  // Subscribed rather than polled: `subscribe` + the version counter is what
  // useSyncExternalStore wants, and it re-renders only when a row is added.
  useSyncExternalStore(subscribe, pbDiagVersion);
  const events = pbDiagEvents();
  // undefined = not asked yet, null = no shell answered (or no shell at all).
  const [shell, setShell] = useState<[string, string][] | null | undefined>(undefined);
  const shellAsked = useRef(false);

  const reportLines = () => {
    const lines = [`la musica playback report — ${new Date().toISOString()}`];
    if (shell === undefined || shell === null) {
      lines.push(`shell\t${shell === null ? t("settings.playback_diag_no_shell") : t("settings.playback_diag_asking")}`);
    } else {
      for (const [k, v] of shell) lines.push(`shell\t${k}\t${v}`);
    }
    for (const ev of events) lines.push(`${clockOf(ev.t)}\t${ev.kind}\t${diagDetailText(ev.detail)}`);
    return lines;
  };

  const copyReport = async () => {
    const text = reportLines().join("\n");
    try {
      await navigator.clipboard.writeText(text);
      toast.success(t("settings.playback_diag_copied", { n: String(events.length) }));
    } catch {
      // The Clipboard API needs a secure context, and this app is normally
      // served over plain http on the LAN, where `navigator.clipboard` is
      // undefined — so the report goes in the message instead of being lost
      // (the same fallback DependenciesPage's `copyCommand` uses for its
      // upgrade command).
      toast.error(`${t("settings.playback_diag_copy_failed")}\n${text}`);
    }
  };

  return (
    <details
      className="mt-3 bg-zinc-950/40 rounded-lg border border-border px-3 py-2"
      onToggle={(e) => {
        if (!e.currentTarget.open || shellAsked.current) return;
        shellAsked.current = true;
        void iosShellState().then(setShell);
      }}
    >
      <summary className="text-xs font-medium cursor-pointer text-zinc-300 select-none">
        {t("settings.playback_diag", { n: String(events.length) })}
      </summary>
      <div className="mt-2 space-y-2">
        <div className="text-[10px] text-zinc-600">{t("settings.playback_diag_help")}</div>

        {/* Shell first: it is the half of the report the page cannot see for
            itself, and its absence is itself an answer. */}
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-wide text-zinc-500">
            {t("settings.playback_diag_shell")}
          </div>
          {shell === undefined ? (
            <div className="text-[11px] text-zinc-600">{t("settings.playback_diag_asking")}</div>
          ) : shell === null ? (
            <div className="text-[11px] text-zinc-600">{t("settings.playback_diag_no_shell")}</div>
          ) : (
            <div className="space-y-1">
              {shell.map(([k, v]) => (
                <div key={k} className="min-w-0">
                  <div className="text-[10px] text-zinc-600 break-all">{k}</div>
                  <div className="font-mono text-xs text-zinc-200 break-all">{v || "—"}</div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-wide text-zinc-500">
            {t("settings.playback_diag_events", { n: String(events.length) })}
          </div>
          {events.length === 0 ? (
            <div className="text-[11px] text-zinc-600">{t("settings.playback_diag_empty")}</div>
          ) : (
            <div className="font-mono text-[11px] leading-relaxed">
              {events.map((ev, i) => (
                <div key={`${ev.t}-${i}`} className="border-t border-border/50 pt-0.5 mt-0.5 first:border-0 first:pt-0 first:mt-0">
                  <span className="text-zinc-500">{clockOf(ev.t)}</span>{" "}
                  <span className="text-zinc-300">{ev.kind}</span>
                  {ev.detail && (
                    <div className="text-zinc-400 break-words">{diagDetailText(ev.detail)}</div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="flex items-center gap-2 flex-wrap pt-1">
          <button className="btn-ghost !py-0.5 text-[11px] tap" onClick={copyReport}>
            <Copy className="h-3 w-3" /> {t("settings.playback_diag_copy")}
          </button>
          <button
            className="btn-ghost !py-0.5 text-[11px] tap"
            onClick={() => {
              pbDiagClear();
              toast(t("settings.playback_diag_cleared"));
            }}
          >
            <Trash2 className="h-3 w-3" /> {t("settings.playback_diag_clear")}
          </button>
        </div>
      </div>
    </details>
  );
}

export default function SettingsPage() {
  const navigate = useNavigate();
  const { data: config, isError: configError } = useQuery({ queryKey: ["config"], queryFn: api.config });
  // Interface zoom — per browser, applied by the app shell (see ZOOM_KEY).
  const [zoom, setZoom] = useState(readZoom);
  // open-source credits (vendored tools + packages), rendered at the bottom
  const { data: credits } = useQuery({
    queryKey: ["credits"],
    queryFn: async () => {
      const r = await fetch("/credits.json");
      return r.json();
    },
    staleTime: Infinity,
  });
  const qc = useQueryClient();
  const [musicFolder, setMusicFolder] = useState("");
  // The folder picker (Library card): a dialog of its own, because choosing
  // the library folder is a different act from editing a form field.
  const [picker, setPicker] = useState(false);
  const [lyricsFormat, setLyricsFormat] = useState("EMBEDDED");
  const [workerLimit, setWorkerLimit] = useState(0);
  // Filled from the config the server normalizes (naming_script is never
  // empty there) — the shipped default is the server's, never a copy here.
  const [namingScript, setNamingScript] = useState("");
  const [shortFolderNames, setShortFolderNames] = useState(false);
  const [accent, setAccent] = useState<string>(() => localStorage.getItem("mlo.accent") ?? DEFAULT_ACCENT);
  // The custom-colour row: the native picker's swatch and the text field are two
  // views of ONE value. `customHex` is always a whole "#rrggbb" (what a Use
  // applies, and what <input type="color"> requires); `customText` is whatever
  // has been typed, so a half-finished "#ff7" is not rewritten mid-keystroke.
  // Both start on the accent in force, so the picker opens where the app is.
  const [customHex, setCustomHex] = useState<string>(() => accentHex(accent));
  const [customText, setCustomText] = useState<string>(() => accentHex(accent));
  // What "back to presets" returns to: the last preset this page applied, so a
  // custom colour is a detour and not a one-way door (a stored custom value
  // leaves the default as the way back).
  const lastPreset = useRef<string>(presetHex(accent) ?? DEFAULT_ACCENT);
  const [defaultView, setDefaultView] = useState<string>(() => localStorage.getItem("mlo.defaultView.v2") ?? "grid");
  const [loaded, setLoaded] = useState(false);
  // Settings search: matches field labels/keys across every tab; picking a
  // result jumps straight to the tab that owns it (computed below, after
  // the group tables exist).
  const [q, setQ] = useState("");
  const searching = q.trim().length >= 2;
  // which password fields are currently revealed
  const [showPasswords, setShowPasswords] = useState<Set<string>>(new Set());

  // `locale` is the locale in force (used by the picker below when the config
  // names none the app ships); `t` labels the picker itself.
  const { t, locale } = useI18n();

  // Provider catalogues behind the order editors (Discovery / Lyrics lists).
  const { data: discoveryCat } = useQuery({ queryKey: ["discoverySources"], queryFn: api.discoverySources });
  const { data: lyricsCat } = useQuery({ queryKey: ["lyricsProviders"], queryFn: api.lyricsProviders });

  // The genre picker offers EXACTLY the chain's own genre sources (the genre
  // rows of /api/sources/health are integrations.GENRE_SOURCES), so a chain
  // change shows up here on its own and no stale id can be ticked. Same cache
  // entry the Sources panel reads, so this costs no extra request.
  const { data: health } = useQuery({
    queryKey: ["sourcesHealth"],
    queryFn: () => api.sourcesHealth(false),
    staleTime: 30000,
  });
  const genreOptions: [string, string][] = (health?.sources ?? [])
    .filter((s) => s.kind === "genre")
    .map((s): [string, string] => [s.id, `${s.label} — ${GENRE_LEVEL[s.id] ?? "per track"}`]);

  // ---- script options (persisted to config; /api/run uses them as defaults) ----
  type CfgField =
    | { k: string; label: string; type: "bool"; help?: string }
    | { k: string; label: string; type: "number"; min?: number; max?: number; step?: number; help?: string }
    | { k: string; label: string; type: "select"; options: [string, string][]; help?: string }
    | { k: string; label: string; type: "text"; help?: string }
    | { k: string; label: string; type: "password"; help?: string }
    /** Ordered provider preference list; an empty list means the built-in order. */
    | { k: string; label: string; type: "list"; catalog: "discovery" | "lyrics"; help?: string }
    /** Unordered set of values (a string list in config) shown as checkboxes. */
    | { k: string; label: string; type: "multi"; options: [string, string][]; help?: string }
    /** Comma-separated list in one text input; kept in config as a string list. */
    | { k: string; label: string; type: "csv"; help?: string };
  interface CfgGroup {
    title: string;
    blurb?: string;
    fields: CfgField[];
  }
  const CFG_GROUPS: CfgGroup[] = [
    {
      title: "AI — lyric transforms & genre ranking",
      blurb:
        "The app's only optional model, shared by script 17's lyric transforms and the genre ranking on the Import & tags tab. Script 17 romanizes non-Latin lyrics and translates them into the languages below, writing TRANSLITERATION-*/TRANSLATION-* tags (and .romaji.lrc / .<lang>.lrc sidecars for LRC/BOTH lyric formats). Any OpenAI-compatible /chat/completions endpoint works — OpenAI, OpenRouter, LM Studio, llama.cpp, or Google Gemini's OpenAI-compatible endpoint (paste the bare generativelanguage.googleapis.com host and it is routed). Nothing else in the app depends on it: with the URL or model empty, the lyric script logs one line and skips, and the genre ranking falls back to the source list. Reasoning effort is sent as `reasoning_effort` on every call — a provider that rejects the field gets one plain retry.",
      fields: [
        { k: "ai_base_url", label: "Base URL", type: "text", help: "e.g. https://api.openai.com/v1, http://localhost:1234/v1, or generativelanguage.googleapis.com" },
        { k: "ai_api_key", label: "API key", type: "password", help: "Sent as a Bearer token. Local servers (LM Studio, llama.cpp) usually ignore it — leave it empty there." },
        { k: "ai_model", label: "Model", type: "text", help: "The model id the endpoint expects, e.g. gpt-4o-mini or gemini-2.5-flash." },
        {
          k: "ai_effort", label: "Reasoning effort", type: "select",
          options: [["high", "High — best quality (default)"], ["medium", "Medium"], ["low", "Low"], ["minimal", "Minimal — no thinking, fastest"]],
        },
        { k: "lyrics_translation_langs", label: "Translation languages", type: "text", help: "Comma separated, e.g. en,de. The first is the reader's language (it decides when romanization is worth generating) and names the TRANSLATION tag; each language also gets its own .<lang>.lrc sidecar." },
        { k: "lyrics_xlit_enabled", label: "Transliterate non-Latin lyrics", type: "bool" },
        { k: "lyrics_translate_enabled", label: "Translate lyrics", type: "bool" },
        { k: "lyrics_xlit_sidecars", label: "Write .romaji.lrc / .<lang>.lrc sidecars (LRC formats only)", type: "bool" },
        { k: "force_xlit", label: "Force: re-transform tracks that already have one", type: "bool" },
      ],
    },
    {
      title: "Library codec (script 3)",
      blurb: "The audio format the whole library ends up as — the same conversion runs on imports. By default a LOSSLESS source is converted to the target while a lossy one is left exactly as it is: lossy → lossless cannot restore a sample, and lossy → lossy is a generation loss. A converted original is moved to Trash, never deleted.",
      fields: [
        {
          k: "library_codec", label: "Library codec", type: "select",
          // The list lives in lib/codecMeta.ts — the same one the setup wizard
          // and the Python side (mlo.containers.CODECS) are pinned to, so a
          // codec added there cannot be missing here.
          options: CODEC_CHOICES,
          help: "What every file in the library ends up as. Lossless targets: FLAC (the shipped default — "
                + "what the verification tools are built around), ALAC (.m4a), WAV, AIFF. Lossy targets: "
                + "MP3 and AAC (CBR), Ogg Vorbis and Opus. \"Keep\" never converts anything.",
        },
        {
          k: "library_codec_optimize", label: "What the optimisation pass may convert", type: "select",
          help: "The default converts a lossless source to the target above and leaves lossy sources alone. "
                + "\"Anything\" also re-encodes lossy sources (a generation loss) — it still refuses a lossy "
                + "source under a lossless target, which can only lose quality.",
          options: [
            ["lossless_to_lossy", "Lossless → the target (lossy left alone) — default"],
            ["all", "Anything → the target"],
            ["keep", "Never convert"],
          ],
        },
        {
          k: "library_codec_bitrate", label: "Lossy bitrate (kbps) / Vorbis quality", type: "number", min: 0, max: 512,
          help: "Applies to the lossy targets only: kbps for MP3/AAC/Opus, Vorbis' own 0-10 quality scale for "
                + "Ogg. 0 uses the codec's own default (MP3 320, AAC 256, Ogg 6, Opus 128).",
        },
        {
          k: "library_codec_quality", label: "Lossless compression level (FLAC 0-8)", type: "number", min: 0, max: 8,
          help: "Used by the FLAC encode and re-encode. ALAC and the PCM targets have no such control and ignore it.",
        },
        {
          k: "library_codec_args", label: "Extra encoder arguments (appended verbatim)", type: "text",
          help: "flac.exe options when the target is FLAC, ffmpeg options for every other target — added after "
                + "the quality/bitrate flags, so your own flag wins. They are not validated: a bad flag fails the "
                + "encode of that file, and the run reports it per file.",
        },
        { k: "lossless_remove_original", label: "Move the converted original to Trash", type: "bool" },
        { k: "add_seektables", label: "Add seektables", type: "bool" },
        { k: "flac_preserve_picture", label: "Preserve embedded picture", type: "bool" },
        { k: "flac_no_padding", label: "No padding", type: "bool" },
        { k: "force_reencode_flac", label: "Force re-encode", type: "bool" },
      ],
    },
    {
      title: "Embedded covers",
      blurb: "Off by default: optimization removes embedded art from audio files — covers live on disk as cover.* / sidecars. When on, the album cover is embedded into every track instead.",
      fields: [
        { k: "embed_covers", label: "Embed covers into audio files", type: "bool" },
        { k: "embed_cover_jpeg_quality", label: "Embedded JPEG quality (JPEG embeds only)", type: "number", min: 60, max: 100 },
        { k: "embed_cover_resolution", label: "Embedded cover max resolution (px, 0 = original)", type: "number", min: 0, max: 4000 },
      ],
    },
    {
      title: "Images (script 5)",
      blurb: "Requires libjxl in .dependencies for JPEG XL conversion.",
      fields: [
        { k: "reencode_images", label: "Re-encode images (master switch)", type: "bool" },
        { k: "rename_to_cover", label: "Rename album art to cover.*", type: "bool" },
        { k: "reencode_to_jxl", label: "Convert images to JPEG XL", type: "bool" },
        { k: "convert_jxl_back", label: "Convert JXL back to original", type: "bool" },
        { k: "images_convert_to_jpeg", label: "Convert other formats to JPEG", type: "bool" },
        { k: "images_convert_lossless_to_png", label: "Convert lossless to PNG", type: "bool" },
        { k: "remove_alpha", label: "Remove PNG alpha", type: "bool" },
        { k: "jpeg_progressive", label: "Progressive JPEG", type: "bool" },
        { k: "jpegxl_effort", label: "JPEG XL effort", type: "number", min: 1, max: 10 },
        { k: "jpegxl_distance", label: "JPEG XL distance (0 = lossless)", type: "number", min: 0, max: 2, step: 0.1 },
        { k: "images_jpeg_quality", label: "JPEG quality", type: "number", min: 70, max: 100 },
        { k: "png_optimization_level", label: "PNG optimization level", type: "number", min: 0, max: 6 },
        { k: "cover_jpeg_quality", label: "Cover JPEG quality", type: "number", min: 70, max: 100 },
        {
          k: "cover_auto_fetch", label: "Fetch missing covers during import", type: "bool",
          help: "During import, an album with no image gets cover candidates looked up (Cover Art Archive first, then Deezer/Apple). Off leaves the finder and the Grading screen's Missing cover verdict to you.",
        },
        {
          k: "cover_review", label: "…and let me pick which one (off = take the best automatically)", type: "bool",
          help: "On: the candidates are shown on the album page for you to choose, and nothing is written until you do. Off: the best candidate is downloaded and normalised on the spot, as before. Ignored while 'Fetch missing covers during import' is off.",
        },
        { k: "cover_resize_enabled", label: "Resize covers", type: "bool" },
        { k: "cover_target_size", label: "Cover target size (px)", type: "number", min: 0, max: 4000 },
        { k: "cover_crop_enabled", label: "Crop covers to square", type: "bool" },
        { k: "cover_crop_threshold", label: "Crop threshold (aspect deviation)", type: "number", min: 0, max: 0.5, step: 0.05 },
        { k: "cover_force_exact_size", label: "Force exact target size", type: "bool" },
        { k: "cover_enforce_size", label: "Enforce size in grading", type: "bool" },
        { k: "cover_enforce_square", label: "Enforce square in grading", type: "bool" },
        { k: "cover_jpeg_enabled", label: "Process JPEG covers", type: "bool" },
        { k: "cover_png_enabled", label: "Process PNG covers", type: "bool" },
        { k: "cover_jxl_enabled", label: "Process JXL covers", type: "bool" },
        { k: "cover_jpeg_target_size", label: "JPEG cover size override (0 = global)", type: "number", min: 0, max: 4000 },
        { k: "cover_png_target_size", label: "PNG cover size override (0 = global)", type: "number", min: 0, max: 4000 },
        { k: "cover_jxl_target_size", label: "JXL cover size override (0 = global)", type: "number", min: 0, max: 4000 },
        // The cover FINDER defaults (region + source list) live in the
        // "Cover art" panel below, which reads the real region list and the
        // per-search source cap from /api/cover/sources.
        { k: "force_reencode_images", label: "Force re-process", type: "bool" },
      ],
    },
    {
      title: "Lyrics & CUEs (scripts 1, 2 & 18)",
      fields: [
        { k: "optimize_lrc", label: "Optimize .lrc sidecars", type: "bool" },
        { k: "optimize_embedded_lyrics", label: "Optimize embedded lyrics", type: "bool" },
        {
          k: "lyrics_sources", label: "Lyrics providers (order)", type: "list", catalog: "lyrics",
          help: "Providers are tried top to bottom when lyrics are fetched from an album, artist or track page. Providers left out of the list are never used.",
        },
        {
          k: "lyrics_allow_plain", label: "Accept plain (unsynced) lyrics", type: "bool",
          help: "Off by default: the chain prefers synced lyrics and writes an untimed one only when no source states timestamps, and this install then fails that track's lyrics check (“Plain” on the track's own surfaces). Turn this on to accept untimed text — LRCLIB's plain records — as a good answer here.",
        },
        {
          k: "lyrics_search_aliases", label: t("settings.lyrics_search_aliases"), type: "bool",
          help: t("settings.lyrics_search_aliases_help"),
        },
        {
          k: "lrclib_auto_publish", label: "Auto-publish missing lyrics to LRCLIB", type: "bool",
          help: "Script 18 (and every import chain that includes it) submits this library's own lyrics to LRCLIB for tracks the database does not have yet — artist, title, album and duration decide that, and a track LRCLIB already answers for is never touched. Outward-facing: with it off, nothing is ever submitted automatically (the manual 'Publish to LRCLIB' button on the lyrics editor still works).",
        },
        {
          k: "force_publish", label: "Force: re-submit lyrics LRCLIB already has", type: "bool",
          help: "One-shot per run — re-publishes even when LRCLIB answers for the track, e.g. when this library's text is the better one. LRCLIB may still reject the duplicate.",
        },
        {
          k: "lyrics_youtube_captions", label: "Use YouTube captions (yt-dlp)", type: "bool",
          help: "Time-synced captions, but only for tracks that carry a YouTube id — the id the video download records — so this never searches YouTube for a track. Automatic captions are used when a video has no typed subtitles and can mishear; needs yt-dlp under Dependencies, otherwise the provider is skipped.",
        },
        { k: "lrc_timestamp_precision", label: "Timestamp precision (decimals)", type: "number", min: 2, max: 3 },
        { k: "lrc_strip_metadata", label: "Strip metadata tags ([ti:], [ar:])", type: "bool" },
        { k: "lrc_collapse_blank_lines", label: "Collapse blank lines", type: "bool" },
        { k: "lrc_enhanced_enabled", label: "Enhanced LRC (word timestamps)", type: "bool" },
        { k: "lrc_enhanced_word_sync", label: "Enhanced LRC word sync", type: "bool" },
        {
          k: "lrc_sync_level", label: "Required lyrics sync level", type: "select",
          options: [["LINE", "Line timestamps only (default)"], ["WORD", "Word"], ["SYLLABLE", "Syllable"]],
        },
        { k: "lrc_extended_enabled", label: "Extended LRC (E-LRC)", type: "bool" },
        { k: "lrc_add_zero_timestamp", label: "Add [00:00.00] opening line", type: "bool" },
        { k: "lrc_zero_timestamp_blank", label: "Zero timestamp is blank line", type: "bool" },
        { k: "lrc_zero_timestamp_target", label: "Zero timestamp target", type: "select", options: [["EMBEDDED", "Embedded"], ["LRC", "LRC sidecar"], ["BOTH", "Both"]] },
        { k: "append_final_newline", label: "Append final newline", type: "bool" },
        { k: "keep_empty_cue_lines", label: "Keep empty CUE lines", type: "bool" },
        { k: "keep_other_cue_lines", label: "Keep non-track CUE lines", type: "bool" },
        { k: "keep_empty_accurip_lines", label: "Keep empty .accurip lines", type: "bool" },
        { k: "cue_file_type", label: "CUE file type", type: "select", options: [["WAVE", "WAVE"], ["MP3", "MP3"]] },
        { k: "force_lyrics", label: "Force lyrics re-format", type: "bool" },
        { k: "force_cue", label: "Force CUE re-format", type: "bool" },
      ],
    },
    {
      title: "DR / ReplayGain (script 7)",
      fields: [
        { k: "dr_replaygain_enabled", label: "Enabled", type: "bool" },
        {
          k: "replaygain_mode", label: "Gain mode", type: "select",
          options: [["track", "Track gain"], ["album", "Album gain"], ["off", "Off — no gain applied"]],
        },
        { k: "replaygain_preamp_db", label: "Preamp (dB)", type: "number", min: -24, max: 24, step: 0.5 },
        {
          k: "replaygain_analyze_missing", label: "Measure tracks without ReplayGain tags instead of playing them at unity",
          type: "bool",
        },
        { k: "replaygain_clip_protection", label: "Clip protection", type: "bool" },
        { k: "replaygain_skip_existing", label: "Skip files that already have RG tags", type: "bool" },
        { k: "force_dr_replaygain", label: "Force re-run", type: "bool" },
      ],
    },
    {
      title: "Audit (script 6)",
      blurb: "AudioAuditor verdict + CD rip verification (REAL/FAKE).",
      fields: [
        { k: "audit_thorough", label: "Thorough mode (full-track detectors)", type: "bool" },
        { k: "audit_cutoff_allow", label: "Frequency cutoff allowance (Hz, 0 = default)", type: "number", min: 0, max: 24000 },
        { k: "audit_verify_cd_checksums", label: "Verify CD .log CRC checksums", type: "bool" },
        { k: "audit_cd_require_both", label: "Require log CRC AND auditor for REAL", type: "bool" },
        { k: "audit_integrity", label: "Verify file integrity (flac -t / decode)", type: "bool" },
        { k: "audit_fail_on_unscorable_log", label: "Fail on unscorable .log", type: "bool" },
        { k: "audit_verify_log_checksum", label: "Verify .log checksum", type: "bool" },
        { k: "audit_require_accuraterip", label: "Require AccurateRip data", type: "bool" },
        { k: "audit_log_score_threshold", label: "Log score threshold", type: "number", min: 0, max: 100 },
        { k: "audit_batch_size", label: "Batch size", type: "number", min: 50, max: 500 },
        { k: "audit_batch_timeout_s", label: "Batch timeout (s)", type: "number", min: 10, max: 120 },
        { k: "audit_per_file_timeout_s", label: "Per-file timeout (s)", type: "number", min: 10, max: 60 },
        { k: "audit_clipping", label: "Detect clipping", type: "bool" },
        { k: "audit_scaled_clipping", label: "Detect scaled clipping", type: "bool" },
        { k: "audit_mqa", label: "Detect MQA", type: "bool" },
        { k: "audit_ai", label: "Detect upscaled audio", type: "bool" },
        { k: "audit_fake_stereo", label: "Detect fake stereo", type: "bool" },
        { k: "audit_silence", label: "Detect silence", type: "bool" },
        { k: "audit_dynamic_range", label: "Measure dynamic range", type: "bool" },
        { k: "audit_true_peak", label: "Measure true peak", type: "bool" },
        { k: "audit_lufs", label: "Measure LUFS", type: "bool" },
        { k: "audit_bpm", label: "Measure BPM", type: "bool" },
        { k: "audit_check_cd_format", label: "Verify CD format (16/44.1)", type: "bool" },
        { k: "force_audit", label: "Force re-audit", type: "bool" },
      ],
    },
    {
      title: "AutoTag (script 8)",
      fields: [
        { k: "auto_advisory", label: "Set advisory automatically", type: "bool" },
        { k: "advisory_auto_fetch", label: "Fetch the advisory rating automatically (import + advisory fetch)", type: "bool" },
        {
          k: "advisory_ai_classify", label: "Judge the lyrics with the AI provider", type: "bool",
          help: "When no source states an advisory at all, the configured AI model reads the track's lyrics "
                + "and answers 0/1/2 (3 = it cannot tell, which falls through to the fallback). Needs an AI "
                + "base URL and model in the AI section; a value a source already stated is never second-guessed.",
        },
        {
          k: "advisory_fallback", label: "When nothing states an advisory", type: "select",
          help: "The last resort for a track no source stated anything about and the AI could not rate.",
          options: [
            ["0", "Store 0 — not explicit"],
            ["2", "Store 2 — clean edition"],
            ["none", "Store nothing — leave it unrated"],
          ],
        },
        { k: "mood_enabled", label: "Write mood tags", type: "bool" },
        {
          k: "genre_autofill", label: "Trim genres to the configured count", type: "bool",
          help: "Genres are never imported by a script: the import (MusicBrainz/RateYourMusic per track, "
                + "then the configured sources) and manual edits are the only writers. Script 8 only brings "
                + "a list longer than the genre count back down to it.",
        },
        {
          k: "mood_source", label: "Mood source", type: "select",
          options: [
            ["audio", "Audio analysis"],
            ["provider", "Provider metadata"],
            ["hybrid", "Hybrid — audio, trusting the genre when the audio is ambiguous"],
          ],
        },
        { k: "auto_instrumental", label: "Set INSTRUMENTAL automatically", type: "bool" },
        { k: "auto_zero_advisory_for_instrumental", label: "Zero advisory on instrumentals (no words, no explicit content)", type: "bool" },
        { k: "fix_instrumental_from_lyrics", label: "Fix INSTRUMENTAL from lyrics", type: "bool" },
        { k: "force_auto_tag", label: "Force re-tag", type: "bool" },
        { k: "force_mood", label: "Force mood & energy re-analysis (script 16)", type: "bool" },
        { k: "instrumental_auto_fetch", label: "Look the INSTRUMENTAL verdict up when nothing states one", type: "bool" },
      ],
    },
    {
      title: "Release tracklist (script 15)",
      fields: [
        { k: "force_tracklist", label: "Force rewrite of an existing .mlo_expected.json", type: "bool" },
      ],
    },
    {
      title: "AccurateRip (script 9)",
      fields: [
        { k: "write_accurip_files", label: "Write .accurip files", type: "bool" },
        { k: "force_accurip", label: "Force re-generate", type: "bool" },
      ],
    },
    {
      title: "Tag writes (global switches)",
      blurb: "Which tag families may be written at all. Per-filetype overrides and encoder marker tags are below.",
      fields: [
        { k: "write_audit_tag", label: "Write AUDIT verdicts", type: "bool" },
        { k: "write_log_grade", label: "Write LOG_GRADE scores", type: "bool" },
        { k: "write_replaygain_tags", label: "Write ReplayGain tags", type: "bool" },
        { k: "write_dynamic_range_tags", label: "Write DR tags", type: "bool" },
        { k: "write_rating_tags", label: "Write RATING tags (your stars)", type: "bool",
          help: "Rating a track also writes RATING (0-100, Picard's scale: one half-star = 10) into the file, "
                + "and clearing a rating removes the tag. Off, ratings stay in the app only. Keeping it on is what "
                + "makes an imported Picard-rated library and this app agree." },
        { k: "normalize_media_source", label: "Normalize MEDIA / SOURCE", type: "bool" },
        { k: "strip_source_on_cd", label: "Strip SOURCE on CD rips", type: "bool" },
        { k: "fill_empty_source", label: "Fill empty SOURCE on digital", type: "bool" },
        { k: "digital_media_source_value", label: "Digital SOURCE value", type: "text" },
      ],
    },
    {
      title: "CD Rips (scripts 2/6/9/10)",
      blurb: "Deterministic CD-N renaming of .log/.cue/.accurip and conservative CUE FILE-name fixes.",
      fields: [
        { k: "discs_rename_enabled", label: "Auto-rename disc sheets to CD-{n}", type: "bool" },
        { k: "discs_rename_single_fallback", label: "Rename lone sheet in single-disc album", type: "bool" },
        { k: "discs_rename_pattern", label: "Rename pattern (must contain {n})", type: "text" },
        { k: "discs_toc_tolerance_s", label: "TOC tolerance (s)", type: "number", min: 0.5, max: 10, step: 0.5 },
        { k: "discs_toc_unique_margin_s", label: "TOC unique margin (s)", type: "number", min: 0.5, max: 10, step: 0.5 },
        { k: "cue_fix_filenames", label: "Fix CUE FILE lines to real files", type: "bool" },
      ],
    },
    {
      title: "Grading (script 4)",
      blurb: "Which file types are allowed in an album and which checks count as failures.",
      fields: [
        { k: "grade_include_music", label: "Allow audio files", type: "bool" },
        { k: "grade_include_cover", label: "Allow cover images", type: "bool" },
        { k: "grade_include_cue", label: "Allow CUE files", type: "bool" },
        { k: "grade_include_log", label: "Allow LOG files", type: "bool" },
        { k: "grade_include_lrc", label: "Allow LRC files", type: "bool" },
        { k: "grade_include_accurip", label: "Allow .accurip files", type: "bool" },
        { k: "grade_include_description", label: "Allow album description files (description.txt)", type: "bool" },
        { k: "grade_include_video", label: "Allow remuxed videos (MKV)", type: "bool" },
        { k: "grade_include_other", label: "Allow other files", type: "bool" },
        { k: "grade_check_raw_video", label: "Fail un-remuxed videos (VOB/AVI...)", type: "bool" },
        { k: "grade_check_lossless_source", label: "Fail uncompressed sources (WAV...)", type: "bool" },
        { k: "grade_log_score_threshold", label: "Log score threshold", type: "number", min: 0, max: 100 },
        { k: "grade_check_log_checksum", label: "Check log checksum", type: "bool" },
        { k: "grade_check_accuraterip", label: "Check AccurateRip", type: "bool" },
        { k: "grader_cover_size_tolerance_px", label: "Cover size tolerance (px)", type: "number", min: 0, max: 5 },
        { k: "grader_strict_square_threshold", label: "Strict square threshold", type: "number", min: 0, max: 0.05, step: 0.005 },
        { k: "grade_verbose", label: "Verbose grading diagnostics", type: "bool" },
      ],
    },
    {
      title: "Videos (script 11)",
      blurb: "Lossless remux: any video container → MKV with the video copied bit-exact and lossless audio converted to FLAC (level below); lossy audio (AC3/DTS/AAC) is copied rather than inflated into FLAC unless that is turned off. Captions/subtitles are always kept and verified — never removed. If the muxer refuses the video codec, H.264 is a last-resort fallback (off by default: it re-encodes the only copy). The original (e.g. the .VOB) is removed after a verified remux.",
      fields: [
        { k: "youtube_enabled", label: "Fetch missing music videos from YouTube", type: "bool", help: "The master switch for every YouTube download: the album header's film button and a track's \"Download music video\" action both refuse to search while it is off. Script 11 itself never searches YouTube — it remuxes the video files already in the folder." },
        { k: "youtube_max_height", label: "Maximum video height (px, 0 = best available)", type: "number", min: 0, max: 4320 },
        {
          k: "youtube_cookies_mode", label: "Cookies for YouTube", type: "select",
          options: [["none", "None — anonymous"], ["file", "A cookies file (saved below)"], ["browser", "Read from a browser"]],
          help: "Your own YouTube session is the only thing that opens an age-gated or members-only video — without it YouTube answers \"Sign in to confirm your age\" — and it stops the throttling a fresh IP gets. None: nothing is sent. A cookies file: the jar saved in the box below this row — paste a cookies.txt into it or drop the file on it, in Netscape format, which is what a browser-extension exporter like \"Get cookies.txt\" writes. Read from a browser: yt-dlp opens that browser's own cookie store — same machine, signed in to YouTube, and closed if its store is locked.",
        },
        {
          k: "youtube_cookies_browser", label: "Browser to read cookies from", type: "select",
          options: [["chrome", "Chrome"], ["chromium", "Chromium"], ["edge", "Edge"], ["firefox", "Firefox"], ["brave", "Brave"], ["opera", "Opera"], ["safari", "Safari"], ["vivaldi", "Vivaldi"], ["whale", "Whale"]],
          help: "Which browser \"Read from a browser\" opens, using its DEFAULT profile. Pick the one you are signed in to YouTube with.",
        },
        { k: "video_reencode_incompatible", label: "Allow H.264 video fallback (lossy re-encode, last resort)", type: "bool" },
        { k: "video_lossy_audio_copy", label: "Copy lossy audio streams instead of re-encoding to FLAC", type: "bool" },
        { k: "video_crf", label: "H.264 CRF (lower = better)", type: "number", min: 0, max: 51 },
        { k: "video_preset", label: "H.264 preset", type: "select", options: [["ultrafast","ultrafast"],["superfast","superfast"],["veryfast","veryfast"],["faster","faster"],["fast","fast"],["medium","medium"],["slow","slow"],["slower","slower"],["veryslow","veryslow"]] },
        { k: "video_flac_level", label: "FLAC compression (0-8)", type: "number", min: 0, max: 8 },
        { k: "video_remove_original", label: "Remove original after verified remux", type: "bool" },
        { k: "video_process_mp4", label: "Also re-mux MP4s into MKV", type: "bool" },
      ],
    },
    {
      title: "Key & BPM (script 12)",
      blurb: "Detects tempo and musical key with librosa and writes BPM + INITIALKEY. Install librosa from the Dependencies tab.",
      fields: [
        { k: "audiometa_enabled", label: "Enabled", type: "bool" },
        { k: "audiometa_overwrite", label: "Overwrite existing BPM/key values", type: "bool" },
        { k: "audiometa_min_seconds", label: "Skip tracks shorter than (seconds)", type: "number", min: 1, max: 120 },
        { k: "audiometa_key_notation", label: "Key notation", type: "select", options: [["musical", "Musical (A min)"], ["camelot", "Camelot (8A)"], ["openkey", "Open Key (1m)"]] },
        { k: "force_audiometa", label: "Force re-analysis", type: "bool" },
      ],
    },
    {
      title: "Soulseek (managed slskd)",
      blurb: "Shares = the library folder (<music folder>/Artists). Downloads land in the download dir below; use the Soulseek page to search and download. Install slskd from the Dependencies tab.",
      fields: [
        { k: "soulseek_username", label: "Soulseek username", type: "text" },
        { k: "soulseek_password", label: "Soulseek password", type: "password" },
        { k: "soulseek_description", label: "Profile description (shown to other users)", type: "text" },
        { k: "soulseek_listen_port", label: "Listen port", type: "number", min: 1024, max: 65535 },
        {
          k: "soulseek_upnp", label: "Open the listen port on the router automatically", type: "bool",
          help: "Asks the router to forward the listen port to this machine whenever slskd starts, replaces the mapping when the port changes, and removes it when this is turned off. Without a router that answers UPnP or NAT-PMP it does nothing at all — the Soulseek page's port state names which it was: mapped, refused (with the router's own reason), or no gateway answered. Turn it off when you forward the port yourself.",
        },
        {
          k: "soulseek_router_ip", label: "Router IP for port mapping (blank = auto-detect)", type: "text",
          help: "UPnP finds its gateway by multicast and NAT-PMP by the machine's default gateway — inside a container that address is Docker's bridge, not the router, so neither method reaches the box that forwards the port. Naming the router here makes the app send the UPnP search and the NAT-PMP requests straight to it. Blank keeps auto-detect.",
        },
        { k: "soulseek_web_port", label: "Web/API port", type: "number", min: 1024, max: 65535 },
        { k: "soulseek_up_limit", label: "Upload speed limit (kB/s, 0 = unlimited)", type: "number", min: 0, max: 100000 },
        { k: "soulseek_down_limit", label: "Download speed limit (kB/s, 0 = unlimited)", type: "number", min: 0, max: 100000 },
        {
          k: "soulseek_download_slots", label: "Concurrent download slots (slskd)", type: "number", min: 1, max: 20,
          help: "How many transfers slskd runs at once — the OUTER ceiling, and the only one of the three numbers that is slskd's rather than this app's. The app enforces `Releases … at once` × `Candidate downloads per release` (Wishes tab) itself; at the shipped defaults that product is 3 × 3 = 9, which is why this defaults to 9. Set it below the product and the app narrows each release's batch to fit (`slots ÷ releases`), so nothing you configure here ends up queued inside slskd.",
        },
        { k: "soulseek_upload_slots", label: "Concurrent upload slots (empty/0 = slskd's own default, 10)", type: "number", min: 0, max: 20 },
        { k: "soulseek_upload_limit_kib", label: "Per-transfer upload limit (KiB/s, 0 = unlimited)", type: "number", min: 0, max: 1000000 },
        { k: "soulseek_download_limit_kib", label: "Per-transfer download limit (KiB/s, 0 = unlimited)", type: "number", min: 0, max: 1000000 },
        {
          k: "soulseek_web_https", label: "Serve the slskd web UI over HTTPS (extra listener, self-signed)", type: "bool",
        },
        { k: "soulseek_download_dir", label: "Download dir (blank = <music folder>/.mlo/downloads)", type: "text" },
        {
          k: "soulseek_clear_downloads", label: "Delete the downloaded copy after a successful import", type: "bool",
          help: "The album is moved into the library first, so the downloaded folder is only staging — deleting it avoids a second full copy of everything you acquire. Only ever the folder the job itself downloaded into, and only after the import reported success: a FAILED import keeps its files so it can be retried without downloading them again.",
        },
        { k: "soulseek_autostart", label: "Start slskd with the app backend", type: "bool" },
        { k: "soulseek_share_library", label: "Share the library folder on the network", type: "bool" },
        { k: "soulseek_share_dirs", label: "Extra shared folders (; separated, blank = the library folder <music>/Artists)", type: "text" },
        { k: "soulseek_share_exclude", label: "Never share these paths (; separated)", type: "text" },
      ],
    },
    {
      title: "Storage & cleanup",
      blurb:
        "What the app may keep on disk in the two folders it fills by itself. Both are scratch space — downloads/staging holds transfers until they are imported, the trash holds what was removed from the library — so neither may grow without end: over its cap, one is emptied OLDEST FIRST until it fits, and nothing in use is ever touched (a transfer still running, a folder an import or a script run is working on). The two caps are separate numbers; they never share a total.",
      fields: [
        {
          k: "soulseek_cache_cap_gb", label: "Soulseek download cache cap (GB, 0 = no cap)", type: "number", min: 0, max: 1000, step: 0.1,
          help: "The download folder plus the staging sibling slskd writes partials into (<music folder>/.mlo/downloads and its `incomplete`), measured together. Over this size the app deletes from them, oldest entry first, until they are back under — the copy a successful import already removes is not what this is for. An entry a transfer is still running in, or one an import / script run holds, is skipped and reported instead. 0 = no cap.",
        },
        {
          k: "trash_cap_gb", label: "Trash cap (GB, 0 = no cap)", type: "number", min: 0, max: 1000, step: 0.1,
          help: "The remove-from-library bin (<music folder>/.mlo/trash), capped on its own — filling the download cache never eats the bin's room. Over this size the app deletes the OLDEST trashed entries for good (exactly like Delete on the Trash page; what is left stays restorable) until the bin is under again, and an entry being restored is skipped. 0 = the bin keeps everything.",
        },
      ],
    },
    {
      title: "Auto-import (MusicBrainz → Soulseek)",
      blurb: "Search terms are templates of release fields (artist album year date country catalognumber barcode label). CD rips are found by catalog number, digital media by title + year; every disc's .log must reach the score threshold before the album downloads.",
      fields: [
        {
          k: "soulseek_auto_physical_queries", label: "Physical query templates (; separated)", type: "text",
          help: "A physical pressing — a CD included — is searched by its catalog number and barcode by default, the traits that name the exact pressing. Add templates (semicolon-separated) to widen the search; a pressing that states neither falls back to its label and country, never to an artist/title query (which asks the network for every other pressing of the album).",
        },
        {
          k: "soulseek_auto_cd_queries", label: "CD query templates (; separated)", type: "text",
          help: "Wins for a CD you set it for: this CD is searched by these templates instead of the physical ones above. Blank follows the physical defaults (catalog number + barcode) — the shipped default here (the catalog number alone) was a catalog-number-only search, which the physical default already covers.",
        },
        {
          k: "soulseek_auto_digital_queries", label: "Digital query templates (; separated)", type: "text",
          help: "Digital Media is searched by these — artist, album and year by default — because it carries no pressing trait to be identified by.",
        },
        {
          k: "soulseek_auto_mbid_queries", label: "Also search by MBIDs", type: "bool",
          help: "On by default. Adds the release's own MusicBrainz id, its tracks' recording ids and each of those tracks' own artist + title to the SAME parallel batch as the templates above, so a peer folder that names the ids — or holds exactly the album's tracks under a name the templates never match — is found too. One search window either way.",
        },
        {
          k: "soulseek_auto_mbid_tracks", label: "Tracks chased by MBID / name (1–10)", type: "number", min: 1, max: 10,
          help: "How many of the release's tracks are chased that way, first track first (disc/position order). Each one is asked for by its recording MBID and by its own artist + title.",
        },
        { k: "soulseek_auto_log_min_score", label: "Min .log score (0–100)", type: "number", min: 0, max: 100 },
        { k: "soulseek_auto_complete_ratio", label: "Required track completeness (0.5–1)", type: "number", min: 0.5, max: 1, step: 0.05 },
        { k: "soulseek_auto_search_wait", label: "Fallback search window (seconds of quiet on a rare album)", type: "number", min: 5, max: 300 },
        { k: "soulseek_fallback_candidates", label: "Candidates tried per release (best first)", type: "number", min: 1, max: 10,
          help: "How many of a release group's ranked editions one search walks: the best first, then the next. The release-choice policy ranks them (status, medium, completeness, original date), so a rare pressing no longer costs you the album — the search moves on to the next edition instead. 1 turns the walk off (the best edition only). A group with fewer eligible editions than this simply ends at the end of its own list." },
        { k: "soulseek_search_timeout_seconds", label: "Search window per candidate (seconds)", type: "number", min: 5, max: 300,
          help: "How long ONE candidate's search is given before it counts as not found and the walk moves on to the next edition — per candidate, so a walk of five may wait up to five of these. It is the same kind of quiet window as the fallback search window above (slskd ends a search when the network stops answering, plus the app's own response grace), and a usable folder still ends a candidate's search in seconds. A walk whose candidates all come back empty is not dropped: the release stays in the Background list and keeps being searched." },
        {
          k: "soulseek_auto_response_limit", label: "Responses before a search is scored (5–500)", type: "number", min: 5, max: 500,
          help: "slskd only hands back a search's results once it has ENDED, and a popular album never goes quiet — this ends the search early instead of waiting out the whole window. Lower = faster and fewer peers; higher = slower and more candidates.",
        },
        { k: "auto_import_avoid_promo", label: "Never auto-import promotional / bootleg editions", type: "bool" },
        {
          k: "auto_import_require_country", label: "Only auto-import editions with a release country", type: "bool",
          help: "A MusicBrainz release without RELEASECOUNTRY is usually an unsorted import, and the CD query templates are built from that field — such editions are skipped, and a group whose only editions lack one is reported as ineligible instead.",
        },
        {
          k: "prefer_release_country", label: "Preferred release country (ISO code, blank = none)", type: "text",
          help: "The spelling MusicBrainz publishes on the release, e.g. US or GB. A tie-breaker only: it never outranks status, medium, track count or the original-edition rule.",
        },
        {
          k: "prefer_original_edition", label: "Prefer the original (explicit) edition over a clean or edited one", type: "bool",
          help: "MusicBrainz states this in the release title or its disambiguation comment. Off, a clean edition is ranked on the other rules like any other — a clean edition may carry altered audio.",
        },
        {
          k: "prefer_disc_streams", label: "Prefer a disc's own streams over a compressed re-encode", type: "bool",
          help: "A BDRip/DVDRip/x264 release is a lossy derivative of the disc, and so is the 700 MB re-encode a rip sometimes ships beside its VIDEO_TS or BDMV folder. On (the default), an edition that names itself one ranks below the disc's own streams — a remux, a full disc — and a folder holding a disc structure beside a re-encode is remuxed as the disc's own single title, with the derivative left where it is. Off, the disc handling goes with it: those files take the ordinary per-file path and nothing prefers the disc's streams — this one switch covers both the download and the file pipeline.",
        },
        {
          k: "auto_import_medium_order", label: "Medium preference (comma-separated, best first)", type: "csv",
          help: "Editions are ranked by this media order first, then by how close the edition is to the release group's original date; a format not named here ranks after every configured one. Blank = the built-in order (CD, Vinyl, Cassette, Other, DVD, Blu-ray, VHS, Video CD, LaserDisc, Digital Media) — CD first, the other physical media next (the video carriers included, so a music video on a disc beats the same video published as a download), digital last.",
        },
      ],
    },
    {
      title: "Wishes (auto-fill)",
      blurb:
        "Releases saved to the library without downloading them. The background worker re-searches Soulseek for every open wish on the interval below and imports a release the moment a verified match appears.",
      fields: [
        { k: "wishes_enabled", label: "Run the wishes worker", type: "bool" },
        {
          k: "soulseek_candidate_slots", label: "Candidate downloads per release", type: "number", min: 1, max: 20,
          help: "How many candidate peers of ONE release may download at the same time (3 by default). The first that verifies good becomes the import and the others are cancelled and swept, and the NEXT candidate is only asked for when one of them lands or fails — so however many candidates a search turns up, one release never talks to more peers than this. Enforced by the app's own enqueueing; slskd's download slots (Soulseek tab) are only the outer ceiling on the transfers it produces.",
        },
        {
          k: "soulseek_search_concurrency", label: "Releases searched / downloaded at once", type: "number", min: 1, max: 8,
          help: "Over this ceiling a release is NOT refused: it takes its place in the queue (Queue → Waiting, with its position) and starts by itself the moment one of the running releases finishes. The wishes worker fills up to this many wishes per pass, and a bulk auto-import run keeps this many jobs in flight. What it does not do on its own is open more connections: that is what `Candidate downloads per release` (per release) and slskd's own download slots add up to.",
        },
        {
          k: "wishes_interval_hours", label: "Search interval (hours)", type: "number", min: 1, max: 168,
          help: "How long to wait between two searches of ONE release — an hour by default. The gap is between two ATTEMPTS: one attempt already asks every ranked edition of the release, each with its own bounded search window, and stops at the first that lands.",
        },
        { k: "wishes_max_attempts", label: "Max attempts per wish (0 = forever)", type: "number", min: 0, max: 1000 },
        {
          k: "wishes_not_found_attempts", label: "Empty searches before a wish is 'not found' (0 = never give up)", type: "number", min: 0, max: 1000,
          help: "A search that finds nothing is not a failure to try harder: it spends one of these. When they are used up the wish ends as 'not found' — terminal, announced once, and searched again only when you press retry on its queue row (or Search on the wish). The album is still unfillable by hand: importing it yourself resolves the wish on the next reconcile.",
        },
        {
          k: "wishes_retry_backoff_minutes", label: "Wait before retrying a failed wish (minutes, doubles per attempt)", type: "number", min: 0, max: 1440,
          help: "A TRANSIENT failure — slskd refused or absent, a MusicBrainz outage, a download that failed verification — waits this long before the next attempt, doubling each time (capped at 24 h). 0 retries on the next interval instead.",
        },
        { k: "wishes_auto_import", label: "Auto-import when a verified match is found", type: "bool" },
        { k: "soulseek_auto_wish_prompt", label: "Keep searching wishes automatically while the app runs", type: "bool" },
        {
          k: "soulseek_auto_lossy_policy", label: "When only lossy copies exist (a wish, a watched artist)", type: "select",
          options: [
            ["never", "Never take one — keep searching (default)"],
            ["best", "Take the best one, and say so"],
          ],
          help: "An unattended download (a wish, an artist watch) that finds only MP3/AAC folders. Never is the shipped behaviour: the release stays on the wish list and keeps being searched, never quietly turning up as lossy audio. Best takes the top-ranked lossy folder the ranking already offers — the fastest, most complete copy of the album — and names the format in the job's log, its queue row and the notification it ends with. The Soulseek page always ASKS you either way, so this can never overrule an answer you gave by hand.",
        },
      ],
    },
    {
      title: "Artist watch",
      blurb:
        "Follow an artist instead of re-checking them by hand: the worker asks MusicBrainz for releases after the watch was created and queues what matches. Nothing is queued twice, an undated release group is never 'new', and the per-cycle cap is what keeps a first check from dumping a back catalogue into the queue.",
      fields: [
        { k: "artist_watch_enabled", label: "Watch artists for new releases", type: "bool" },
        { k: "artist_watch_interval_hours", label: "Check interval (hours)", type: "number", min: 1, max: 720, help: "How long between two checks of the SAME artist; the worker itself ticks far more often." },
        { k: "artist_watch_max_per_cycle", label: "Releases queued per artist per check", type: "number", min: 1, max: 50, help: "The hard anti-dump cap. One is the shipped default: a watch that queued a hundred at once is the discography dump this feature exists to avoid." },
        { k: "artist_watch_types", label: "Release types a watch may queue", type: "multi", options: [["album","Album"],["ep","EP"],["single","Single"],["broadcast","Broadcast"],["other","Other"],["compilation","Compilation"],["soundtrack","Soundtrack"],["spokenword","Spoken word"],["interview","Interview"],["audiobook","Audiobook"],["live","Live"],["remix","Remix"],["dj-mix","DJ mix"],["mixtape/street","Mixtape / street"],["demo","Demo"],["audio drama","Audio drama"],["field recording","Field recording"],["podcast","Podcast"]], help: "MusicBrainz's own type names, plus the app's derived one: Podcast (a podcast is a MusicBrainz SERIES, and an episode is a Broadcast release group linked to it — not a release-group type MusicBrainz publishes). A release group matches when its primary type is ticked or ANY secondary type is (a live album is Album + Live, so ticking Live finds it); Podcast matches only a group whose series relation the app read. A watch can narrow this per artist." },
        { k: "artist_watch_auto_add", label: "Queue a matched release into the library automatically", type: "bool", help: "Off, a watch only reports what it found (the notification is the whole output) — which is what you want if you pick the edition by hand." },
      ],
    },
    {
      title: "Downloads & playback",
      blurb:
        "What a downloaded (offline) copy holds, and which copy plays. Downloading caches a track on this device so it plays with the server away — see the Downloads page. Quality here is about the CACHE: the library's own audio is never touched, and streaming always serves the library file.",
      fields: [
        {
          k: "download_codec", label: "Downloaded copies are", type: "select",
          options: [["copy", "Copy (the file's own codec) — default"], ...CODEC_CHOICES.filter(([v]) => v !== "keep")],
          help: "Copy stores exactly what the library holds — nothing is re-encoded, so a track is downloaded in the codec it is already in. "
                + "Any other target re-encodes the track for this device's cache only (smaller downloads for a phone; the library file keeps its own format). "
                + "There is no \"keep\" here: copy IS never re-encode.",
        },
        {
          k: "download_bitrate", label: "Download bitrate (kbps) / Vorbis quality", type: "number", min: 0, max: 512,
          help: "The re-encode's rate: kbps for MP3/AAC/Opus, Vorbis' own 0-10 quality scale for Ogg. 0 uses the codec's own "
                + "default (MP3 320, AAC 256, Ogg 6, Opus 128). Ignored while the codec above is Copy, and by a lossless target.",
        },
        {
          k: "playback_source", label: "Play tracks from", type: "select",
          options: [["stream", "Streaming from the server — default"], ["downloaded", "The downloaded copy"]],
          help: "Streaming asks the server for the library file even when a copy is downloaded; the downloaded copy plays what is cached, "
                + "saving bandwidth and working with the server away. Either way a copy plays when the server cannot be reached.",
        },
      ],
    },
    {
      title: "Server & remote access",
      blurb: "Where this server listens and the address clients should dial. A change to the port or host is picked up at the next start; the address is what a phone or desktop client is told to use.",
      fields: [
        { k: "download_concurrency", label: "Files read at once for one download (1–8)", type: "number", min: 1, max: 8, help: "The reader pool behind a queue or offline-cache download: higher fills a socket faster, at the cost of staging more file data in memory." },
        { k: "server_host", label: "Listen address (0.0.0.0 = every interface)", type: "text", help: "Anything but 127.0.0.1 means other machines can reach this server — the login gate turns itself on there (see the Security tab)." },
        { k: "server_port", label: "Port", type: "number", min: 1, max: 65535 },
        { k: "server_public_url", label: "Public address clients should use (blank = this machine)", type: "text" },
      ],
    },
    {
      title: "Home",
      blurb:
        "The Home section in the sidebar — its shelves are built from the library itself (recently added, best graded, top artists, favorites, wants, needs attention) with nothing fetched online.",
      fields: [
        { k: "home_recent_count", label: "Recently-added albums shown", type: "number", min: 4, max: 60 },
      ],
    },
    {
      title: "Discovery",
      blurb:
        "Online providers (Deezer, ListenBrainz, MusicBrainz, Last.fm, Wikipedia…) used for artist images and album/artist descriptions. Each order list is tried top to bottom; the first provider with a usable answer wins. An empty list means the built-in order shown as the placeholder.",
      fields: [
        {
          k: "artist_image_sources", label: "Artist image sources (order)", type: "list", catalog: "discovery",
          help: "Used when fetching an artist image automatically; the picked image can still be overridden per artist.",
        },
        {
          k: "description_sources", label: "Description sources (order)", type: "list", catalog: "discovery",
          help: "Used for artist and album descriptions.",
        },
        { k: "discovery_timeout_s", label: "Request timeout (s)", type: "number", min: 3, max: 30 },
        {
          k: "rym_cookie", label: "RateYourMusic cookie", type: "password",
          help: "Only needed when RYM answers with a challenge. Two ways in: the import panel below takes a cookies.txt in Netscape format — what a browser-extension exporter like \"Get cookies.txt\" writes — pasted into the box or dropped on it, and keeps only its rateyourmusic.com cookies; or open the devtools route — sign in to rateyourmusic.com, press F12 → Network → reload → click any request to rateyourmusic.com → Headers → Request Headers → copy everything after \"Cookie:\" and paste it in the field above (newlines and the \"Cookie:\" label are handled for you). RYM's `session` cookie is HttpOnly, so a browser extension's export is the only way to get it out of a browser at all. It is a session credential — do not share it, and paste a fresh one when RYM starts refusing, since signing out or clearing cookies invalidates it. Blank = RYM is skipped like any other unavailable source; MusicBrainz still resolves RYM links for well-known releases. Test it with the Sources panel's Test button.",
        },
        {
          k: "rym_links_auto", label: "Auto-find RateYourMusic links", type: "bool",
          help: "Asks rateyourmusic.com for the album and artist pages during an import (and from the link editor's Auto-find button). An existing link is never overwritten, and when RYM refuses the request the import carries on untouched — the link is then left for you to paste by hand.",
        },
        {
          k: "spotify_client_id", label: "Spotify client ID (optional)", type: "text",
          help: "Optional second advisory source (Spotify's ISRC lookup) behind Deezer and ahead of Apple. Empty = Spotify is skipped; an import never fails without it.",
        },
        {
          k: "spotify_client_secret", label: "Spotify client secret (optional)", type: "password",
          help: "Pairs with the client ID above — both are needed before the Spotify lookup runs.",
        },
        { k: "discovery_enabled", label: "Use online discovery providers", type: "bool", help: "Off, the Home shelves and every artist/album lookup answer from the library and MusicBrainz alone: no Deezer, ListenBrainz, Last.fm or Wikipedia request leaves the machine." },
      ],
    },
    {
      title: "Artist images & descriptions",
      blurb:
        "Artwork and text that live next to the audio: artist photos stored with the artist, and descriptions stored in non-destructive tags. Grading can require them (see the Grading tab).",
      fields: [
        { k: "metadata_auto_fetch", label: "Fetch artist image / descriptions on import", type: "bool" },
        { k: "metadata_review", label: "Review metadata candidates before writing them", type: "bool" },
        { k: "artist_image_enabled", label: "Fetch artist images", type: "bool" },
        { k: "artist_image_crop", label: "Crop artist images to the configured aspect", type: "bool" },
        // The Settings page's own CfgField text member carries no pattern pair
        // (only configMeta's does, and the wizard is what consumes it), so this
        // row keeps the shape in its help text instead.
        {
          k: "artist_image_aspect", label: "Artist image aspect (W:H)", type: "text",
          help: "Width:height, e.g. 1:1 (square), 4:5, 16:9. The shape artist images are stored in: the fetch crops to it, grading fails an image further than 2% from it, and script 19 (Optimize artist images) crops the ones already in the library back to it.",
        },
        { k: "artist_image_target_size", label: "Artist image max size (px, 0 = keep native size)", type: "number", min: 0, max: 4000 },
        { k: "artist_description_enabled", label: "Fetch artist descriptions", type: "bool" },
        { k: "album_description_enabled", label: "Fetch album descriptions", type: "bool" },
        { k: "description_full", label: "Fetch the full description text (not just the summary)", type: "bool" },
      ],
    },
    {
      title: "Import pipeline",
      blurb:
        "What happens after an album lands in the library (Soulseek downloads, Drag & drop, Finish import). The script chain below runs in order; leaving it blank runs the built-in chain: dedupe → sort → tag → covers → lyrics → audit → ReplayGain → AccurateRip. AcoustID fingerprints the audio to identify the exact release — it needs a free application key from acoustid.org; without one, matching falls back to title/artist/genre against MusicBrainz.",
      fields: [
        {
          k: "auto_acquisition_enabled", label: "Automatic acquisition (searching and downloading on their own)", type: "bool",
          help: "Off, nothing the app starts by itself searches or downloads: the wishes worker stops its passes, an artist watch queues nothing, and \"Add to library\" records the album and its wish without starting a download. What you asked for is still recorded and a check says the switch is off rather than \"nothing found\" — the wish's own Search now, the wizard and the Soulseek page still work, because those are you acting, not the app.",
        },
        {
          k: "manual_import_enabled", label: "Manual importing (the wizard and POST /api/import/*)", type: "bool",
          help: "Off, the import wizard and every importing /api/import/* route refuse with a sentence naming this setting instead of importing — the wizard shows that sentence where its steps would be. The automatic pipeline still imports what it downloads; only the paths you drive by hand are turned off.",
        },
        {
          k: "import_autonomy", label: "Import autonomy", type: "select",
          options: [
            ["automatic", "Automatic — decide everything the sources can answer (default)"],
            ["review", "Review — stop at each step that needs a decision"],
          ],
          help: "Automatic runs the whole chain and only comes back to you for what nothing could supply: the album is imported either way and whatever it still lacks is reported as ONE prompt — a notification plus an entry the Import page lists — naming the families and linking to the album at the step where each decision is made. Review is the wizard's own behaviour applied to an import: it stops before the first step that needs a decision (a family the album is still missing) and hands the album over instead of deciding past it.",
        },
        {
          k: "import_review_families", label: "Decide by hand, even when automatic", type: "multi",
          options: [["links", "Links"], ["cover", "Cover art"], ["genres", "Genres"], ["lyrics", "Lyrics"], ["advisory", "Advisory"]],
          help: "Families an import must never decide for you, whatever the mode above. A cover kept here has its candidates staged instead of writing the first hit; the links and advisory fetches are skipped; lyrics drop out of the chain. The rest of the import stays automatic, and the album's prompt names that family as waiting for you rather than as unsourced.",
        },
        {
          k: "import_keep_synced_lyrics", label: "Keep a synced lyric an import arrives with", type: "bool",
          help: "An import replaces the families it decides for itself with what it found: the album's lyrics, genres, advisories and cover art win over whatever the download came with. A family you kept above (or one whose own switch is off) is never touched. This is the lyric family's one exception — ON, a track whose lyric already carries timestamps (a synced one) keeps it and the fetch skips that track; OFF, the shipped default, the peer's lyric is replaced like everything else. A PLAIN (untimed) lyric is always replaced: the providers answer with that form anyway.",
        },
        { k: "import_auto_scripts", label: "Run the script chain after import", type: "bool" },
        {
          k: "import_scripts", label: "Import script ids (e.g. 1, 3, 5, 7 — blank = built-in chain)", type: "text",
        },
        { k: "import_bulk_concurrency", label: "Bulk import concurrency", type: "number", min: 1, max: 8 },
        { k: "import_acoustid", label: "Fingerprint with AcoustID", type: "bool" },
        { k: "acoustid_enabled", label: "AcoustID enabled", type: "bool" },
        { k: "acoustid_api_key", label: "AcoustID application key (free, acoustid.org)", type: "password" },
        {
          k: "acoustid_user_key", label: "AcoustID user key (fingerprint submissions)", type: "password",
          help: "The USER key of your own acoustid.org account (AcoustID → your account → API keys), a different key from the application one above. It is needed ONLY to submit fingerprints: a submission gives AcoustID one fingerprint TOGETHER WITH the MusicBrainz recording id it is (MusicBrainz itself never receives a fingerprint), and it comes from the wizard's AcoustID step, the details menus' 'Submit fingerprints (AcoustID)' entry or Run All once you tick it. A pair AcoustID already links — or one this app already sent — is never re-sent, and a blank or refused key comes back in AcoustID's own words. Looking a release up never uses it — the application key alone can do that. The Test button in Settings → Sources proves the key with one probe submission.",
        },
        {
          k: "acoustid_fpcalc_path", label: "fpcalc path (blank = bundled/next to the app)", type: "text",
          help: "AcoustID fingerprints audio by running Chromaprint's fpcalc. The app looks for it next to itself and on PATH by default; point this at the binary when it lives somewhere else (a manual install, a package manager's prefix). A path that does not run is reported as \"could not answer\" on the AcoustID step rather than as a no-match — the check is unverified, not rejected.",
        },
        { k: "acoustid_min_score", label: "Minimum AcoustID match score", type: "number", min: 0, max: 1, step: 0.05 },
      ],
    },
    {
      title: "Import & tag cleanup",
      blurb: "Genre importing from MusicBrainz and tag hygiene applied while optimizing. The genre inference below runs once per album during an import (with a model configured in the AI section), so its effort costs a slower import, never a slower app.",
      fields: [
        {
          k: "mb_genre_count", label: "Genres per track (import, trimming and grading)", type: "number", min: 1, max: 3,
          help: "One value, three consumers: an import writes up to this many genres onto a track (specific genres first, the derived FAMILY last), script 8 / the genre import / script 10 trim any excess off, and grading fails a track carrying more than this. Fewer is fine — the family is derived from the specific genre, so one specific genre is a complete answer and nothing is topped up with filler. A per-run import limit may only lower this. Default 2.",
        },
        {
          k: "genre_sources", label: "Genre sources — every ticked source is asked; unticked ones are never used", type: "multi",
          // The list IS the backend chain (the genre rows of
          // /api/sources/health = server/integrations.GENRE_SOURCES), and each
          // row says whether it can answer per track or only for the release.
          options: genreOptions,
          help: "The genres the sources answer with are merged, deduped and capped at the count above, per track. MusicBrainz is the app's own identity anchor — it also supplies the family every list ends with — so leave it on in most setups.",
        },
        {
          k: "rym_archive_fallback", label: "RateYourMusic: fall back to archived pages", type: "bool",
          help: "A release rateyourmusic.com will not serve — no cookie, or a refused request — is read from the Wayback Machine's snapshot of the SAME page and parsed the same way, so the genres still come from RYM; the import report names the snapshot and its date. Off, RateYourMusic contributes nothing without a cookie.",
        },
        {
          k: "ai_genre_inference", label: "Let a model rank the genres", type: "bool",
          help: "The model is given what the sources above already answered and picks which of them describe the track, most specific first. Needs a base URL and model in the AI section; with none configured the source list is used as it stands.",
        },
        {
          k: "ai_genre_effort", label: "Reasoning effort for that ranking", type: "select",
          options: [["high", "High — reason, then research what the list misses (default)"], ["medium", "Medium"], ["low", "Low"], ["minimal", "Minimal — no thinking, fastest"]],
        },
        {
          k: "ai_genre_research", label: "Let the model go beyond the fetched genres", type: "bool",
          help: "On, the model may name a genre the sources did not answer with when it knows the artist better than they do — the name must still be a MusicBrainz genre to survive. Off, the answer is strictly a re-ranking of what was fetched.",
        },
        { k: "strip_unknown_tags", label: "Remove non-canonical tags on optimize (script 10)", type: "bool" },
      ],
    },
    {
      title: "Beets tagging (script 14)",
      blurb: "Managed beets import with Picard-parity behaviors: MusicBrainz matching, locale alias translations, WORK/MOVEMENT from work relationships, and release-type capitalization (EP uppercased). Files are organized by your naming script via the mlo_dir path hook. Runs as part of Run All; skipped quietly when beets isn't installed.",
      fields: [
        { k: "locale", label: "Locale for names and aliases (e.g. en, ja, de)", type: "text",
          help: "The ONE locale the app writes and shows names in: the MusicBrainz"
            + " pages' aliases, the beets import's alias translations and the"
            + " Soulseek alias searches all read this value." },
        { k: "beets_translations", label: "Translate titles/names to preferred locale", type: "bool" },
        { k: "beets_work_movement", label: "Write WORK / MOVEMENT from work relationships", type: "bool" },
        { k: "beets_release_type_caps", label: "Capitalize release types (EP uppercased)", type: "bool" },
        { k: "beets_organize_after", label: "Re-run organize after each beets import", type: "bool" },
      ],
    },
    {
      title: "Dependencies",
      blurb:
        "External tools are pinned to reviewed releases; the table above shows what upstream has published as well. This is the only switch that belongs to the tool chain itself.",
      fields: [
        {
          k: "dependencies_auto_update", label: "Install missing tools and updates automatically", type: "bool",
          help: "A background pass every few hours installs every tool whose state is Missing or Update — into the dependencies folder, nothing system-wide. Off by default: the app downloads binaries on its own schedule only if you ask it to. The button above still works either way.",
        },
      ],
    },
  ];
  const GRADE_CHECK_KEYS: CfgField[] = [
    { k: "grade_check_tag_spaces", label: "Tag spaces", type: "bool" },
    { k: "grade_check_tag_case", label: "Tag value case", type: "bool" },
    { k: "grade_check_lyrics_spaces", label: "Lyrics spaces", type: "bool" },
    { k: "grade_check_cue_spaces", label: "CUE spaces", type: "bool" },
    { k: "grade_check_cover_crop", label: "Cover aspect ratio (squareness)", type: "bool" },
    { k: "grade_check_lyrics_zero", label: "Lyrics zero timestamp", type: "bool" },
    { k: "grade_check_tag_blank_lines", label: "Tag blank lines", type: "bool" },
    { k: "grade_check_lyrics_blank_lines", label: "Lyrics blank lines", type: "bool" },
    { k: "grade_check_cue_blank_lines", label: "CUE blank lines", type: "bool" },
    { k: "grade_check_unreadable", label: "Unreadable files", type: "bool" },
    { k: "grade_check_missing_tags", label: "Missing tags", type: "bool" },
    { k: "grade_check_encoder", label: "Encoder markers", type: "bool" },
    { k: "grade_check_audit", label: "AUDIT present", type: "bool" },
    { k: "grade_check_instrumental", label: "INSTRUMENTAL", type: "bool" },
    { k: "grade_check_lyrics", label: "Lyrics present", type: "bool" },
    { k: "grade_check_lyrics_format", label: "Lyrics format", type: "bool" },
    { k: "grade_check_sidecar_cover", label: "Sidecar cover", type: "bool" },
    { k: "grade_check_mb_links", label: "MusicBrainz links (album/artist/track)", type: "bool" },
    { k: "grade_check_rym_links", label: "RateYourMusic links (album/artist/track)", type: "bool" },
    { k: "grade_check_media", label: "MEDIA tag", type: "bool" },
    { k: "grade_check_source", label: "SOURCE tag", type: "bool" },
    { k: "grade_check_album_tags", label: "Album-level tags", type: "bool" },
    { k: "grade_check_cd_log", label: "CD log", type: "bool" },
    { k: "grade_check_cd_cue", label: "CD cue", type: "bool" },
    { k: "grade_check_disc_naming", label: "Disc naming", type: "bool" },
    { k: "grade_check_log_grade", label: "Log grade", type: "bool" },
    { k: "grade_check_crc", label: "CRC", type: "bool" },
    { k: "grade_check_cd_format", label: "CD format", type: "bool" },
    { k: "grade_check_cover", label: "Cover present", type: "bool" },
    { k: "grade_check_cue_format", label: "CUE format", type: "bool" },
    { k: "grade_check_cue_files", label: "Per-track CUE sheets (a CUE beside the tracks)", type: "bool" },
    { k: "grade_check_accurip_format", label: ".accurip format", type: "bool" },
    { k: "grade_check_expected_tracks", label: "Release tracklist manifest (albums carrying a MusicBrainz release id)", type: "bool" },
    { k: "grade_check_disallowed", label: "Disallowed files", type: "bool" },
    { k: "grade_check_extra_images", label: "Extra artwork (images not tied to a track)", type: "bool" },
    { k: "grade_check_empty_folders", label: "Empty folders (no files anywhere beneath them)", type: "bool" },
    { k: "grade_check_naming", label: "Naming script paths", type: "bool" },
    { k: "grade_check_filename_case", label: "Filename capitalization (exact case)", type: "bool" },
    { k: "grade_check_ext_case", label: "Lowercase file extensions", type: "bool" },
    { k: "grade_check_excess_tags", label: "Excess tags (non-canonical)", type: "bool" },
    { k: "grade_check_key_bpm", label: "Key & BPM tags", type: "bool" },
    { k: "grade_check_lyrics_lang_tags", label: "Transform tags carry language (TRANSLATION-EN)", type: "bool" },
    { k: "grade_check_mood", label: "Mood tag present", type: "bool" },
    { k: "grade_check_energy", label: "Energy tag present (0-100, with MOOD)", type: "bool" },
    { k: "grade_check_genre", label: "Genre tag present", type: "bool" },
    {
      k: "grade_check_genre_count", label: "Genre count per track (at most mb_genre_count)", type: "bool",
      help: "A track may hold at most the 'Genres per track' value — only an overflow fails (issue code GENRE_COUNT). There is no lower bound and no quota; keep the two in step.",
    },
    {
      k: "grade_check_genre_order", label: "Genre order (the family, if present, comes first)", type: "bool",
      help: "The family must be the FIRST genre, e.g. Rock / Shoegaze (issue code GENRE_ORDER). A family in a later slot, or a genre repeated, fails. The names themselves are graded by the vocabulary check below.",
    },
    {
      k: "grade_check_genre_vocab", label: "Genre vocabulary (MusicBrainz)", type: "bool",
      help: "Every GENRE name must be one MusicBrainz publishes (shoegaze, dream pop, …); an unknown name fails with issue code GENRE_VOCAB and is named in the report. Grading never rewrites the tag — run Auto tagging (8) or Format all (10) to canonicalize.",
    },
    { k: "grade_check_album_description", label: "Album description stored", type: "bool" },
    { k: "grade_check_artist_image", label: "Artist image stored", type: "bool" },
    { k: "grade_check_artist_description", label: "Artist description stored", type: "bool" },
    { k: "grade_check_replaygain", label: "ReplayGain tags present (only when a file already carries one)", type: "bool" },
    { k: "grade_check_acoustid", label: "AcoustID tags present (only when a file already carries one)", type: "bool" },
  ];
  // Toggles the General tab renders by hand (they belong to no group tab) —
  // listed here so they load, save and search like every other setting.
  const GENERAL_TOGGLES: CfgField[] = [
    { k: "auto_advance", label: "Auto-advance between Run All scripts", type: "bool" },
    { k: "show_sidecar_files", label: "Show sidecar files (cue/log/lrc/accurip) in library", type: "bool" },
  ];
  const ALL_CFG_KEYS = [
    ...CFG_GROUPS.flatMap((g) => g.fields),
    ...GRADE_CHECK_KEYS,
    ...GENERAL_TOGGLES,
  ]
    .map((f) => f.k)
    // The language picker in the General tab is hand-rendered (its options come
    // from the i18n bundle, not from the config), but it still loads and saves
    // through `scriptCfg` like every other setting.
    // The notification switches are config keys this page owns but that are
    // not part of any field group, so they are seeded explicitly — without
    // that they render unchecked while the server has them on (the default),
    // and the first click would set the value to true instead of false.
    .concat("ui_locale", "notify_wish_found", "notify_download_done", "notify_import_ready",
            "notify_soulseek_download_start", "notify_soulseek_upload_start");
  const [scriptCfg, setScriptCfg] = useState<Record<string, unknown>>({});
  const setCfg = (k: string, v: unknown) => setScriptCfg((c) => ({ ...c, [k]: v }));

  /** The locale the picker shows: the config's own value when the app ships a
   *  bundle for it, otherwise whatever is in force — an empty config value, or
   *  one naming a language with no bundle, must not select a blank option. */
  const localeValue = LOCALES.some((l) => l.code === String(scriptCfg.ui_locale))
    ? String(scriptCfg.ui_locale)
    : locale;
  /** Language pick: written to the config like every other General field (via
   *  `scriptCfg`, so Save all settings persists it) and applied at once — this
   *  is the one setting whose effect the user expects before saving. setLocale
   *  also remembers it in this browser, so the choice survives a reload even
   *  before the config round-trip. */
  const pickLocale = (code: string) => {
    setCfg("ui_locale", code);
    setLocale(code);
  };
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [rawConfig, setRawConfig] = useState("{}");
  const [tab, setTab] = useState("general");
  // A link can name the tab it wants: `/settings?tab=discovery` is how the
  // Sources panel's RateYourMusic row reaches that tab's cookie import box.
  // Every pick goes through here and drops the parameter again (the effect
  // under NAV adopts whatever the URL names), so the rail and the URL cannot
  // disagree — and the NEXT click on that link still moves, where navigating
  // to an already-current URL would not.
  const [params, setParams] = useSearchParams();
  const pickTab = (id: string) => {
    setTab(id);
    if (!params.get("tab")) return;
    const next = new URLSearchParams(params);
    next.delete("tab");
    setParams(next, { replace: true });
  };
  // This browser's own notification permission, re-read on mount so the
  // panel shows the truth even when it was granted in another tab.
  const [notifyState, setNotifyState] = useState<NotifyState>(() => notificationState());
  const [runAll, setRunAll] = useState<number[]>(DEFAULT_RUN_ALL);
  const [beetsBusy, setBeetsBusy] = useState(false);
  const { data: beetsStatus, refetch: refetchBeets } = useQuery({
    queryKey: ["beetsStatus"],
    queryFn: api.beetsStatus,
    enabled: tab === "beets",
  });
  const installBeets = async () => {
    setBeetsBusy(true);
    try {
      const r = await api.beetsInstall();
      toast.success(`beets v${r.version} installed`);
      refetchBeets();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBeetsBusy(false);
    }
  };

  // The grid follows the live order (Run All executes exactly this list), so
  // a tick can never move a script in the pipeline without saying so. Scripts
  // that are switched off stay listed after the enabled ones, in factory order
  // — plus the opt-in ones (OPT_IN_SCRIPTS: the outward-facing scripts the
  // shipped chain deliberately does not carry), which have no factory slot at
  // all and would otherwise be invisible here.
  const runAllScripts: { id: number; label: string }[] = [
    ...runAll,
    ...DEFAULT_RUN_ALL.filter((id) => !runAll.includes(id)),
    ...OPT_IN_SCRIPTS.filter((id) => !runAll.includes(id)),
  ].map((id) => ({ id, label: SCRIPT_LABEL[id] ?? `#${id}` }));

  /** Tick / untick a script; a re-ticked script goes back to its default
   * pipeline position instead of jumping to the end. A script the shipped
   * chain does not carry has no such position, so it lands at the end. */
  const toggleRunAllScript = (id: number, on: boolean) =>
    setRunAll((ids) => {
      if (!on) return ids.filter((i) => i !== id);
      const at = DEFAULT_RUN_ALL.includes(id)
        ? ids.filter((i) => DEFAULT_RUN_ALL.indexOf(i) < DEFAULT_RUN_ALL.indexOf(id)).length
        : ids.length;
      return [...ids.slice(0, at), id, ...ids.slice(at)];
    });

  // Every per-script force switch. They also live in their own script tab;
  // both places bind to the same config keys, and the master toggle below
  // flips them all at once.
  const FORCE_KEYS: { k: string; label: string }[] = [
    { k: "force_lyrics", label: "1 · Lyrics re-format" },
    { k: "force_cue", label: "2 · CUE re-format" },
    { k: "force_reencode_flac", label: "3 · FLAC re-encode" },
    { k: "force_reencode_images", label: "5 · Image re-process" },
    { k: "force_audit", label: "6 · Re-audit" },
    { k: "force_dr_replaygain", label: "7 · DR / ReplayGain re-run" },
    { k: "force_auto_tag", label: "8 · AutoTag re-run" },
    { k: "force_accurip", label: "9 · AccurateRip re-generate" },
    { k: "force_audiometa", label: "12 · Key & BPM re-analysis" },
    { k: "force_tracklist", label: "15 · Release tracklist rewrite" },
    { k: "force_mood", label: "16 · Mood & Energy re-analysis" },
    { k: "force_xlit", label: "17 · Lyrics re-transliterate / re-translate" },
    { k: "force_publish", label: "18 · Lyrics re-publish to LRCLIB" },
    // Script 20 is the one force key that turns work OFF rather than redoing
    // it: the layout scan always reports, and this is what lets it rename and
    // move. It belongs in this list all the same — it is a per-library-script
    // switch like the others, the master toggle above must cover every one of
    // them, and the Force menu in the header offers it by the same name
    // (`web/src/lib/force.ts` keeps the two lists in step; it used to be
    // missing here, which left the master toggle unable to turn the fixer off).
    { k: "layout_apply", label: "20 · Layout fix (rename / gather)" },
  ];

  /** The server-side notification switches — one per event kind the backend
   *  publishes (server/events.py). These decide what is published at all; each
   *  client still asks for its own OS permission below. */
  const NOTIFY_KEYS = [
    { k: "notify_wish_found", label: "Wish found on Soulseek" },
    { k: "notify_download_done", label: "Download finished" },
    { k: "notify_import_ready", label: "Album ready to import" },
    { k: "notify_import_start", label: "Import started (moving in, then the chain)" },
    { k: "notify_import_done", label: "Import finished (with the chain's summary)" },
    { k: "notify_soulseek_download_start", label: "Soulseek download started" },
    { k: "notify_soulseek_upload_start", label: "A peer started downloading from you" },
  ];

  const NAV: { id: string; label: string; section?: string }[] = [
    { id: "general", label: "General" },
    { id: "appearance", label: "Appearance" },
    { id: "security", label: t("settings.security") },
    { id: "notifications", label: t("settings.notifications") },
    { id: "remote", label: "Remote access" },
    { id: "downloads", label: "Downloads & playback" },
    { id: "home", label: "Home" },
    { id: "naming", label: "Naming" },
    { id: "storage", label: "Storage" },
    { id: "tagwrites", label: "Tagging" },
    { id: "grading", label: "Grading" },
    { id: "beets", label: "Beets", section: "Integrations" },
    { id: "soulseek", label: "Soulseek", section: "Integrations" },
    { id: "autoimport", label: "Auto-import", section: "Integrations" },
    { id: "wishes", label: "Wishes", section: "Integrations" },
    { id: "artistwatch", label: "Artist watch", section: "Integrations" },
    { id: "deps", label: "Dependencies", section: "Integrations" },
    { id: "discovery", label: "Discovery", section: "Providers" },
    { id: "sources", label: "Sources", section: "Providers" },
    { id: "artistimages", label: "Artist images", section: "Providers" },
    { id: "ai", label: "AI", section: "Providers" },
    { id: "import", label: "Import", section: "Providers" },
    { id: "flac", label: "FLACs & lossless", section: "Scripts" },
    { id: "embedcovers", label: "Embedded covers", section: "Scripts" },
    { id: "images", label: "Images", section: "Scripts" },
    { id: "lyrics", label: "Lyrics & CUEs", section: "Scripts" },
    { id: "dr", label: "DR / ReplayGain", section: "Scripts" },
    { id: "audit", label: "Audit", section: "Scripts" },
    { id: "autotag", label: "AutoTag", section: "Scripts" },
    { id: "accurip", label: "AccurateRip", section: "Scripts" },
    { id: "cdrips", label: "CD Rips", section: "Scripts" },
    { id: "videos", label: "Videos", section: "Scripts" },
    { id: "audiometa", label: "Key & BPM", section: "Scripts" },
    { id: "importtags", label: "Import & tags", section: "Scripts" },
  ];

  // The other half of `pickTab` above: adopt the tab the URL names, on mount
  // and on every later change, so a link into a tab works however the page was
  // reached. A name the rail does not have is dropped — an unknown tab would
  // render an empty column — which is why this reads NAV, declared just above
  // and usable here because the effect runs after the render that built it.
  useEffect(() => {
    const want = params.get("tab") ?? "";
    if (want && NAV.some((n) => n.id === want)) setTab(want);
  }, [params]);

  const runPreview = async () => {
    setPreviewing(true);
    setPreviewError(null);
    try {
      const r = await api.namingPreview(namingScript, shortFolderNames);
      if (r.ok && r.path) {
        setPreviewPath(r.path);
      } else {
        setPreviewError(r.error || "Script produced an empty path");
        setPreviewPath(null);
      }
    } catch (e) {
      setPreviewError(String(e));
      setPreviewPath(null);
    } finally {
      setPreviewing(false);
    }
  };

  // The server's ui_locale takes effect as soon as the config lands — unless
  // this browser already holds the user's own pick, which outranks it (i18n
  // resolves that precedence; this page only hands the config over, and the
  // same call belongs wherever the config query resolves).
  useEffect(() => {
    if (config) applyConfigLocale(config);
  }, [config]);

  useEffect(() => {
    if (!config || loaded) return;
    setMusicFolder(String(config.music_folder ?? ""));
    setLyricsFormat(String(config.lyrics_format ?? "EMBEDDED"));
    setWorkerLimit(Number(config.worker_limit ?? 0));
    setNamingScript(String(config.naming_script ?? ""));
    setShortFolderNames(!!config.short_folder_names);
    setScriptCfg(Object.fromEntries(ALL_CFG_KEYS.map((k) => [k, config[k] ?? CFG_DEFAULTS[k]])));
    const enc = (config.encoder_tags ?? {}) as Record<string, Record<string, boolean>>;
    const aw = (config.audio_tag_writes ?? {}) as Record<string, Record<string, boolean>>;
    setEncoderTags(
      Object.fromEntries(
        ENCODER_FORMATS.map((fmt) => [fmt, Object.fromEntries(ENCODER_FIELDS.map((f) => [f, !!enc[fmt]?.[f]]))])
      )
    );
    setAudioTagWrites(
      Object.fromEntries(
        AUDIO_TYPES.map((t) => [t, Object.fromEntries(TAG_FAMILIES.map((fam) => [fam, aw[t]?.[fam] ?? true]))])
      )
    );
    setRawConfig(JSON.stringify(config, null, 2));
    setRunAll(Array.isArray(config.run_all_order) ? config.run_all_order.map(Number).filter(isScriptId) : DEFAULT_RUN_ALL);
    setLoaded(true);
  }, [config, loaded]);

  /** Apply an accent for this browser: a preset id or a custom "#rrggbb". The
   *  value is stored AND painted — the paint is what makes every other page
   *  (and the canvases, via lib/accent's subscribers) follow along without a
   *  reload. */
  const pickAccent = (id: string) => {
    setAccent(id);
    localStorage.setItem("mlo.accent", id);
    applyAccent(id);
    const hex = presetHex(id);
    // A preset pick moves the custom row onto it too, so the picker and the
    // text field always show the colour actually in force.
    if (hex) {
      lastPreset.current = id;
      setCustomHex(hex);
      setCustomText(hex);
    }
  };

  // The custom row's two derived facts: what the text field currently spells,
  // and whether the accent in force IS a custom colour (a preset id never
  // parses as a hex).
  const typedHex = normalizeHex(customText);
  const customActive = parseHexColor(accent) !== null;

  const pickDefaultView = (v: string) => {
    setDefaultView(v);
    localStorage.setItem("mlo.defaultView.v2", v);
  };

  const { data: configDefaults } = useQuery({
    queryKey: ["configDefaults"],
    queryFn: api.configDefaults,
  });

  /** Restore factory defaults for everything the settings form edits.
   * Identity-critical values the user configured are kept: music folder
   * and first-run flag.
   * Persisted via the normal Save. */
  const resetAllDefaults = () => {
    const d = configDefaults as Record<string, unknown> | undefined;
    if (!d) return;
    const cur = scriptCfg as Record<string, unknown>;
    const next: Record<string, unknown> = { ...d };
    for (const k of ["music_folder", "first_run_done"]) {
      if (cur[k] !== undefined) next[k] = cur[k];
    }
    setScriptCfg(next);
    if (d.encoder_tags) setEncoderTags(d.encoder_tags as Record<string, Record<string, boolean>>);
    if (d.audio_tag_writes) setAudioTagWrites(d.audio_tag_writes as Record<string, Record<string, boolean>>);
    if (typeof d.lyrics_format === "string") setLyricsFormat(d.lyrics_format);
    if (d.worker_limit !== undefined) setWorkerLimit(Number(d.worker_limit));
    if (typeof d.naming_script === "string") setNamingScript(d.naming_script);
    setShortFolderNames(!!d.short_folder_names);
    if (Array.isArray(d.run_all_order)) setRunAll((d.run_all_order as number[]).map(Number));
    toast("Settings reset to defaults — click Save all settings to persist");
  };

  /** Clear every UI preference this browser kept: accent, sidebar collapse,
   *  grid sizes, table column layouts and widths, custom columns, the
   *  fullscreen player's look, and the lyrics editor's key map. None of it
   *  lives in the server config, so "Reset to defaults" above cannot reach
   *  it — and a reload is what makes the built-in defaults apply again. */
  const resetUiLayout = () => {
    const keys = Object.keys(localStorage).filter((k) => k.startsWith("mlo"));
    keys.forEach((k) => localStorage.removeItem(k));
    toast(`Layout reset — ${keys.length} UI preference(s) cleared`);
    setTimeout(() => window.location.reload(), 500);
  };

  const save = async () => {
    try {
      await api.saveConfig({
        ...config,
        ...scriptCfg,
        encoder_tags: encoderTags,
        audio_tag_writes: audioTagWrites,
        music_folder: musicFolder,
        lyrics_format: lyricsFormat,
        worker_limit: workerLimit,
        naming_script: namingScript,
        short_folder_names: shortFolderNames,
        run_all_order: runAll,
      });
      toast.success("Config saved");
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["library"] });
      qc.invalidateQueries({ queryKey: ["importScriptsPreview"] });
    } catch (e) {
      toast.error(String(e));
    }
  };

  const applyRaw = () => {
    try {
      const parsed = JSON.parse(rawConfig) as Record<string, unknown>;
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("expected an object");
      const enc = (parsed.encoder_tags ?? {}) as Record<string, Record<string, boolean>>;
      const aw = (parsed.audio_tag_writes ?? {}) as Record<string, Record<string, boolean>>;
      setScriptCfg(Object.fromEntries(ALL_CFG_KEYS.map((k) => [k, parsed[k] ?? CFG_DEFAULTS[k]])));
      setEncoderTags(
        Object.fromEntries(
          ENCODER_FORMATS.map((fmt) => [fmt, Object.fromEntries(ENCODER_FIELDS.map((f) => [f, !!enc[fmt]?.[f]]))])
        )
      );
      setAudioTagWrites(
        Object.fromEntries(
          AUDIO_TYPES.map((t) => [t, Object.fromEntries(TAG_FAMILIES.map((fam) => [fam, aw[t]?.[fam] ?? true]))])
        )
      );
      setMusicFolder(String(parsed.music_folder ?? musicFolder));
      setLyricsFormat(String(parsed.lyrics_format ?? lyricsFormat));
      setWorkerLimit(Number(parsed.worker_limit ?? workerLimit));
      setNamingScript(String(parsed.naming_script ?? "") || namingScript);
      setShortFolderNames(!!parsed.short_folder_names);
      setRunAll(Array.isArray(parsed.run_all_order) ? parsed.run_all_order.map(Number).filter(isScriptId) : runAll);
      toast("Raw config applied — click Save all settings to persist");
    } catch (e) {
      toast.error("Invalid JSON: " + String(e));
    }
  };

  // Tab → group, matched by title (robust against group reordering).
  const GROUP_BY_TAB: Record<string, CfgGroup> = Object.fromEntries(
    [
      ["flac", "FLACs"], ["embedcovers", "Embedded covers"], ["images", "Images"], ["lyrics", "Lyrics & CUEs"],
      ["dr", "DR / ReplayGain"], ["audit", "Audit"], ["autotag", "AutoTag"],
      ["accurip", "AccurateRip"], ["tagwrites", "Tag writes"],
      ["grading", "Grading"], ["cdrips", "CD Rips"], ["videos", "Videos"],
      ["audiometa", "Key & BPM"], ["beets", "Beets tagging"],
      ["storage", "Storage & cleanup"],
      ["soulseek", "Soulseek (managed slskd)"], ["autoimport", "Auto-import"],
      ["wishes", "Wishes"], ["artistwatch", "Artist watch"], ["remote", "Server & remote access"],
      ["downloads", "Downloads & playback"],
      ["home", "Home"], ["deps", "Dependencies"],
      ["discovery", "Discovery"], ["artistimages", "Artist images"], ["ai", "AI lyric transforms"], ["import", "Import pipeline"],
      ["importtags", "Import & tag cleanup"],
    ].map(([tab, prefix]) => [
      tab,
      CFG_GROUPS.find((g) => g.title.toLowerCase().startsWith(String(prefix).toLowerCase())),
    ])
  ) as Record<string, CfgGroup>;

  const searchHits = (() => {
    if (!searching) return [] as { tab: string; group: string; field: string }[];
    const needle = q.trim().toLowerCase();
    const hits: { tab: string; group: string; field: string }[] = [];
    const tabFor = (title: string) =>
      Object.entries(GROUP_BY_TAB).find(([, g]) => g?.title === title)?.[0] ?? "";
    for (const g of CFG_GROUPS) {
      const tab = tabFor(g.title);
      for (const f of g.fields) {
        if (f.label.toLowerCase().includes(needle) || f.k.toLowerCase().includes(needle)) {
          hits.push({ tab, group: g.title, field: f.label });
        }
      }
    }
    for (const c of GRADE_CHECK_KEYS) {
      if (c.label.toLowerCase().includes(needle) || c.k.toLowerCase().includes(needle)) {
        hits.push({ tab: "grading", group: "Grading checks", field: c.label });
      }
    }
    for (const f of GENERAL_TOGGLES) {
      if (f.label.toLowerCase().includes(needle) || f.k.toLowerCase().includes(needle)) {
        hits.push({ tab: "general", group: "General", field: f.label });
      }
    }
    return hits.slice(0, 24);
  })();

  const [encoderTags, setEncoderTags] = useState<Record<string, Record<string, boolean>>>({});
  const toggleEncoder = (fmt: string, field: string, on: boolean) =>
    setEncoderTags((e) => ({ ...e, [fmt]: { ...e[fmt], [field]: on } }));

  const [audioTagWrites, setAudioTagWrites] = useState<Record<string, Record<string, boolean>>>({});
  const toggleTagWrite = (ftype: string, fam: string, on: boolean) =>
    setAudioTagWrites((a) => ({ ...a, [ftype]: { ...a[ftype], [fam]: on } }));

  const { data: deps, refetch: refetchDeps } = useQuery({
    queryKey: ["dependencies"],
    queryFn: () => api.dependencies(),
    retry: false,
  });
  // What this build can do — asked once per page visit and reused by the
  // dependency table below: it is the only thing that can tell a tool this
  // device cannot run from a tool nobody installed yet.
  const { data: caps } = useQuery({
    queryKey: ["capabilities"],
    queryFn: () => api.capabilities(),
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
  const [depsBusy, setDepsBusy] = useState(false);
  // The dependency row whose own Install/Update press is in flight. The
  // page-level button settles the table through `depsBusy` + `refetchDeps()`
  // (the same mechanism a row press uses); this only decides which row spins.
  const [depBusyKey, setDepBusyKey] = useState<string | null>(null);
  // Why no external tool can run here, or null when the backend can start
  // one. One measurement for the whole table (see api.deviceUnavailable).
  const deviceReason = deviceUnavailable(caps);
  const unavailable = unavailableFeatures(caps);

  /** `keys` undefined = everything missing or behind (the page button);
   *  `keys` = [one tool] from that row's own button. `pressed` is the row that
   *  asked, so it carries the spinner — the settling is the same either way:
   *  this `depsBusy` flag plus `refetchDeps()`, never a state the server has
   *  not answered yet. */
  const installDeps = async (keys?: string[], pressed?: string) => {
    setDepsBusy(true);
    setDepBusyKey(pressed ?? null);
    try {
      const r = await api.installDependencies(keys);
      const failed = r.results.filter((x) => !x.ok);
      if (failed.length) toast.error("Install finished with " + failed.length + " failure(s)");
      else toast.success("Dependencies installed / updated");
      refetchDeps();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setDepsBusy(false);
      setDepBusyKey(null);
    }
  };

  // The rows this tab's page-level button can act on — only what this host can
  // FETCH (see the Dependencies page): a Windows-only tool on Linux, or one
  // the host already provides as a distro package, has nothing to download and
  // is counted as work anywhere.
  const depTools = deps?.tools ?? [];
  const depMissing = depTools.filter((t) => t.state === "missing" && t.installable !== false);
  const depUpdates = depTools.filter((t) => t.state === "update" && t.installable !== false);

  // Which scripts the saved import chain runs (blank import_scripts = built-in).
  const { data: chainPreview } = useQuery({
    queryKey: ["importScriptsPreview"],
    queryFn: () => api.importScriptsPreview([]),
    enabled: tab === "import",
  });

  // Catalogues behind the `list` fields: pickable providers plus the built-in
  // order, shown as the placeholder while a list is empty.
  const listCatalogs: Record<"discovery" | "lyrics", { options: ProviderOption[]; builtin: (k: string) => string[] }> = {
    discovery: { options: discoveryCat?.sources ?? [], builtin: (k) => discoveryCat?.defaults?.[k] ?? [] },
    // Only time-synced providers are pickable: a plain-lyrics-only entry has
    // no place in the chain (the `synced` flag comes from the endpoint).
    lyrics: {
      options: (lyricsCat?.sources ?? []).filter((s) => s.synced !== false),
      builtin: () => lyricsCat?.default_order ?? [],
    },
  };

  const renderFields = (fields: CfgField[]) => (
    <div className="stagger grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-x-6 gap-y-2.5 mt-2">
      {fields.map((f) => {
        if (f.type === "list") {
          const raw = scriptCfg[f.k];
          const order = Array.isArray(raw)
            ? raw.map(String)
            : typeof raw === "string"
              ? raw.split(/[,;\s]+/).filter(Boolean)
              : [];
          return (
            <div key={f.k} className="md:col-span-2 xl:col-span-3">
              <div className="text-[11px] text-zinc-400 mb-1">{f.label}</div>
              <ProviderOrder
                options={listCatalogs[f.catalog].options}
                builtin={listCatalogs[f.catalog].builtin(f.k)}
                order={order}
                onChange={(next) => setCfg(f.k, next)}
                help={f.help}
              />
            </div>
          );
        }
        if (f.type === "multi") {
          const on = Array.isArray(scriptCfg[f.k]) ? (scriptCfg[f.k] as unknown[]).map(String) : [];
          // An id the catalogue does not list (a source the chain dropped, or
          // the health payload not arrived yet) still has to be visible, or a
          // saved value would silently disappear from the picker.
          const opts: [string, string][] = [
            ...f.options,
            ...on.filter((v) => !f.options.some(([o]) => o === v)).map((v): [string, string] => [v, v]),
          ];
          return (
            <div key={f.k} className="md:col-span-2 xl:col-span-3">
              <div className="text-[11px] text-zinc-400 mb-1">{f.label}</div>
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                {opts.map(([v, l]) => (
                  <label key={v} className="flex items-center gap-2 text-[13px] text-zinc-300 cursor-pointer select-none">
                    <input
                      type="checkbox"
                      checked={on.includes(v)}
                      onChange={(e) => setCfg(f.k, e.target.checked ? [...on, v] : on.filter((x) => x !== v))}
                    />
                    {l}
                  </label>
                ))}
              </div>
              {f.help && <div className="text-[10px] text-zinc-600 mt-1">{f.help}</div>}
            </div>
          );
        }
        if (f.type === "csv") {
          // Edited as text, stored as the string list the config expects;
          // blank entries are dropped by the backend's own normalization.
          const parts = Array.isArray(scriptCfg[f.k]) ? (scriptCfg[f.k] as unknown[]).map(String) : [];
          return (
            <div key={f.k} className="md:col-span-2 xl:col-span-3">
              <div className="text-[11px] text-zinc-400 mb-1">{f.label}</div>
              <input
                className="input !py-1 text-xs w-full tap"
                value={parts.join(", ")}
                onChange={(e) => setCfg(f.k, e.target.value.split(",").map((s) => s.trim()))}
              />
              {f.help && <div className="text-[10px] text-zinc-600 mt-1">{f.help}</div>}
            </div>
          );
        }
        return (
        <label key={f.k} className="flex items-center gap-2 text-[13px] text-zinc-300 cursor-pointer select-none">
          {f.type === "bool" ? (
            <div className="w-full">
              <span className="flex items-center gap-2 w-full">
                <input type="checkbox" checked={!!scriptCfg[f.k]} onChange={(e) => setCfg(f.k, e.target.checked)} />
                {f.label}
              </span>
              {f.help && <div className="text-[10px] text-zinc-600 mt-0.5">{f.help}</div>}
            </div>
          ) : f.type === "select" ? (
            <div className="w-full">
              <div className="flex items-center gap-2 w-full">
                <span className="flex-1 min-w-0 truncate">{f.label}</span>
                <select
                  className="input !w-32 !py-0.5 text-[11px] shrink-0 tap"
                  value={String(scriptCfg[f.k] ?? "")}
                  onChange={(e) => setCfg(f.k, e.target.value)}
                >
                  {f.options.map(([v, l]) => (
                    <option key={v} value={v}>{l}</option>
                  ))}
                </select>
              </div>
              {f.help && <div className="text-[10px] text-zinc-600 mt-0.5">{f.help}</div>}
            </div>
          ) : f.type === "text" ? (
            <div className="w-full">
              <div className="flex items-center gap-2 w-full">
                <span className="flex-1 min-w-0 truncate">{f.label}</span>
                <input
                  className="input !w-32 !py-0.5 text-[11px] shrink-0 tap"
                  value={
                    Array.isArray(scriptCfg[f.k])
                      ? (scriptCfg[f.k] as unknown[]).join("; ")
                      : String(scriptCfg[f.k] ?? "")
                  }
                  onChange={(e) => setCfg(f.k, e.target.value)}
                />
              </div>
              {f.help && <div className="text-[10px] text-zinc-600 mt-0.5">{f.help}</div>}
            </div>
          ) : f.type === "password" ? (
            <div className="w-full">
              <div className="flex items-center gap-2 w-full">
                <span className="flex-1 min-w-0 truncate">{f.label}</span>
                <div className="relative shrink-0">
                  <input
                    className="input !w-32 !py-0.5 !pr-7 text-[11px] tap"
                    type={showPasswords.has(f.k) ? "text" : "password"}
                    value={String(scriptCfg[f.k] ?? "")}
                    onChange={(e) => setCfg(f.k, e.target.value)}
                  />
                  <button
                    type="button"
                    className="absolute right-1.5 top-1/2 -translate-y-1/2 p-[9px] -mx-[9px] text-zinc-500 hover:text-zinc-200"
                    title={showPasswords.has(f.k) ? "Hide password" : "Show password"}
                    onClick={() =>
                      setShowPasswords((prev) => {
                        const next = new Set(prev);
                        if (next.has(f.k)) next.delete(f.k);
                        else next.add(f.k);
                        return next;
                      })
                    }
                  >
                    {showPasswords.has(f.k) ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                  </button>
                </div>
              </div>
              {f.help && <div className="text-[10px] text-zinc-600 mt-0.5">{f.help}</div>}
            </div>
          ) : (
            <div className="w-full">
              <div className="flex items-center gap-2 w-full">
                <span className="flex-1 min-w-0 truncate">{f.label}</span>
                <input
                  className="input !w-20 !py-0.5 text-[11px] shrink-0 text-right tap"
                  type="number"
                  min={f.min}
                  max={f.max}
                  step={f.step ?? 1}
                  value={String(scriptCfg[f.k] ?? "")}
                  onChange={(e) => setCfg(f.k, Number(e.target.value))}
                />
              </div>
              {f.help && <div className="text-[10px] text-zinc-600 mt-0.5">{f.help}</div>}
            </div>
          )}
        </label>
        );
      })}
    </div>
  );

  return (
    <div className="p-6 space-y-5 mx-auto max-w-[1600px]">
      <PageHeader icon={SettingsIcon} title="Settings">
        <div className="relative w-full max-w-md">
          <input
            className="input !py-1.5 text-sm w-full"
            placeholder="Search settings… (e.g. cover, lyrics, catalog)"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          {searching && (
            <div className="absolute z-30 left-0 right-0 top-full mt-1 rounded-lg bg-zinc-950 border border-border shadow-2xl max-h-80 overflow-auto p-1.5">
              {searchHits.length === 0 && (
                <div className="px-2.5 py-2 text-xs text-zinc-500">No settings match “{q.trim()}”.</div>
              )}
              {searchHits.map((h, i) => (
                <button
                  key={i}
                  className="w-full text-left px-2.5 py-1.5 rounded-md hover:bg-white/10 tap"
                  onClick={() => {
                    if (h.tab) pickTab(h.tab);
                    setQ("");
                  }}
                >
                  <div className="text-xs text-zinc-200">{h.field}</div>
                  <div className="text-[10px] text-zinc-500">{h.group}</div>
                </button>
              ))}
            </div>
          )}
        </div>
      </PageHeader>

      {/* One column below md: the tab rail becomes a horizontal, scrollable
          strip ABOVE the panels instead of a 176px column beside them, which
          would leave the fields about eight characters wide on a phone. */}
      <div className="flex flex-col gap-4 md:flex-row md:gap-6">
        <nav className="flex shrink-0 gap-1.5 overflow-x-auto pb-1 md:sticky md:top-20 md:self-start md:block md:max-h-[calc(100vh-120px)] md:w-44 md:space-y-0.5 md:overflow-y-auto md:pb-0 md:pr-1">
          {NAV.map((n, i) => (
            <div key={n.id} className="shrink-0">
              {n.section && (i === 0 || NAV[i - 1].section !== n.section) && (
                <div className="hidden md:block px-3 pt-3 pb-1 text-[10px] uppercase tracking-wider text-zinc-600 first:pt-0">{n.section}</div>
              )}
              <button
                onClick={() => pickTab(n.id)}
                className={`w-full whitespace-nowrap text-left px-3 py-2 rounded-md text-xs transition-colors md:py-1.5 ${
                  tab === n.id ? "bg-accent on-accent font-medium" : "text-zinc-400 hover:text-white hover:bg-panel border border-transparent"
                } tap`}
              >
                {n.label}
              </button>
            </div>
          ))}
        </nav>

        <div className="flex-1 min-w-0 space-y-5 pb-10">
          {tab === "general" && (
            <div className="panel space-y-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Library</div>
              <div className="rounded-md border border-border bg-zinc-950/40 px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="text-xs text-zinc-500 uppercase">Music folder</div>
                  <button className="btn-ghost !py-1 text-xs tap" onClick={() => setPicker(true)}>
                    <FolderOpen className="h-3 w-3" /> Change…
                  </button>
                </div>
                <div className="font-mono text-xs text-zinc-200 break-all mt-1">
                  {musicFolder.trim() || "not configured yet"}
                </div>
                <div className="text-[11px] text-zinc-600 mt-1 leading-relaxed">
                  Pick any folder this machine can open — the picker browses it and the choice is saved at once. The
                  app's own state moves into <code>&lt;music&gt;/.mlo</code> with it. <code>MLO_MUSIC_FOLDER</code> (Docker
                  / compose) or <code>music_folder</code> in config.json still work for a first start.
                </div>
              </div>
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">Lyrics format</span>
                <select className="input mt-1" value={lyricsFormat} onChange={(e) => setLyricsFormat(e.target.value)}>
                  <option value="EMBEDDED">Embedded</option>
                  <option value="LRC">LRC sidecar</option>
                  <option value="BOTH">Both</option>
                </select>
              </label>
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">Worker limit (0 = auto)</span>
                <input className="input mt-1" type="number" min={0} value={workerLimit} onChange={(e) => setWorkerLimit(Number(e.target.value))} />
              </label>
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">{t("settings.language")}</span>
                <select className="input mt-1" value={localeValue} onChange={(e) => pickLocale(e.target.value)}>
                  {LOCALES.map((l) => (
                    <option key={l.code} value={l.code}>{l.label}</option>
                  ))}
                </select>
                <span className="text-[11px] text-zinc-600 mt-1 block">{t("settings.language_help")}</span>
              </label>
              <div className="flex flex-wrap gap-x-6 gap-y-1.5">
                {GENERAL_TOGGLES.map((f) => (
                  <label key={f.k} className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                    <input type="checkbox" checked={!!scriptCfg[f.k]} onChange={(e) => setCfg(f.k, e.target.checked)} />
                    {f.label}
                  </label>
                ))}
              </div>
              <details className="bg-zinc-950/40 rounded-lg border border-border px-3 py-2">
                <summary className="text-xs font-medium cursor-pointer text-zinc-400 select-none">
                  Run All — scripts in order ({runAll.length} enabled)
                </summary>
                <div className="grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-1.5 mt-2">
                  {runAllScripts.map((s) => (
                    <label key={s.id} className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={runAll.includes(s.id)}
                        onChange={(e) => toggleRunAllScript(s.id, e.target.checked)}
                      />
                      <span className="text-zinc-600 w-4">{s.id}</span>
                      {s.label}
                    </label>
                  ))}
                </div>
                <div className="text-[10px] text-zinc-600 mt-1">The Run All button executes them in this order.</div>
              </details>
              <div className="rounded-md border border-border bg-zinc-950/40 px-3 py-2 space-y-1.5">
                <div className="text-xs text-zinc-400">Setup wizard</div>
                <div className="text-[11px] text-zinc-600 leading-relaxed">
                  Re-runs the first-run walkthrough — music folder, dependencies, sources, AI &amp;
                  RateYourMusic, Soulseek sharing. Every step is skippable, and nothing on this page is
                  reset by it: it only writes what you enter there.
                </div>
                <button
                  className="btn-ghost !py-1 text-xs tap"
                  onClick={() => navigate("/setup")}
                >
                  <Wand2 className="h-3 w-3" /> Run the setup wizard again
                </button>
              </div>
              <details className="bg-zinc-950/40 rounded-lg border border-border px-3 py-2">
                <summary className="text-xs font-medium cursor-pointer text-zinc-400 select-none">
                  Force options — re-run scripts even when up to date
                </summary>
                <label className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none mt-2 font-medium">
                  <input
                    type="checkbox"
                    checked={FORCE_KEYS.every((f) => !!scriptCfg[f.k])}
                    onChange={(e) =>
                      setScriptCfg((c) => {
                        const next = { ...c };
                        for (const f of FORCE_KEYS) next[f.k] = e.target.checked;
                        return next;
                      })
                    }
                  />
                  Force every script (ignore all &ldquo;already done&rdquo; skips)
                </label>
                <div className="grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-1.5 mt-1.5">
                  {FORCE_KEYS.map((f) => (
                    <label key={f.k} className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={!!scriptCfg[f.k]}
                        onChange={(e) => setCfg(f.k, e.target.checked)}
                      />
                      {f.label}
                    </label>
                  ))}
                </div>
                <div className="text-[10px] text-zinc-600 mt-1">
                  Forced scripts redo work even when output already looks up to date. Each toggle also appears in its script tab; the Run All button has its own one-shot Force switch.
                </div>
              </details>
            </div>
          )}

          {tab === "appearance" && (
            <div className="panel space-y-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Appearance</div>
              {/* Zoom comes first on purpose: index.html turns pinch-zoom OFF,
                  so this is the only way to scale the app on a phone. */}
              <label className="flex items-center justify-between gap-3 text-xs text-zinc-300 tap">
                <span>
                  Interface zoom <span className="text-zinc-600">(80–150%, default 100%)</span>
                </span>
                <span className="flex items-center gap-2">
                  <input
                    type="range"
                    min={80}
                    max={150}
                    step={5}
                    value={zoom}
                    onChange={(e) => {
                      const percent = Number(e.target.value);
                      setZoom(percent);
                      try {
                        localStorage.setItem(ZOOM_KEY, String(percent));
                      } catch {
                        /* private mode: the zoom applies now and is gone on the next load */
                      }
                      // The root font size this scales is main.tsx's (the
                      // shell's file), so tell it rather than setting a style
                      // from here — a second source of truth would drift the
                      // moment the shell reloads on its own.
                      window.dispatchEvent(new CustomEvent<number>("mlo:zoom", { detail: percent }));
                    }}
                    className="w-40 accent-[var(--accent)]"
                  />
                  <span className="w-9 text-right tabular-nums">{zoom}%</span>
                </span>
              </label>
              <div>
                <span className="text-xs text-zinc-500 uppercase">{t("settings.accent")}</span>
                <div className="flex flex-wrap gap-2 mt-1.5">
                  {ACCENT_OPTIONS.map((a) => (
                    <button
                      key={a.id}
                      // The tick's OWN ink comes from the swatch's colour, so it
                      // stays visible on a light preset (the old hardcoded black
                      // vanished on every dark one).
                      style={{
                        backgroundColor: a.hex,
                        borderColor: accent === a.id ? "#fff" : "#3f3f46",
                        color: `rgb(${a.fg})`,
                      }}
                      title={t(ACCENT_LABEL[a.id])}
                      aria-label={t(ACCENT_LABEL[a.id])}
                      aria-pressed={accent === a.id}
                      onClick={() => pickAccent(a.id)}
                      className="h-8 w-8 rounded-lg border-2 flex items-center justify-center transition-transform hover:scale-110 tap"
                    >
                      {accent === a.id && <Check className="h-4 w-4" />}
                    </button>
                  ))}
                </div>
                {/* Custom colour: the native picker and the text field are two
                    views of one value, and the field takes #rgb / #rrggbb with
                    or without the hash — whichever form is to hand. */}
                <div className="flex flex-wrap items-center gap-2 mt-2">
                  <input
                    type="color"
                    value={customHex}
                    title={t("settings.accent_custom")}
                    aria-label={t("settings.accent_custom")}
                    onChange={(e) => {
                      setCustomHex(e.target.value);
                      setCustomText(e.target.value);
                    }}
                    className="h-8 w-10 shrink-0 cursor-pointer rounded-lg border border-border bg-transparent p-0.5"
                  />
                  <input
                    value={customText}
                    spellCheck={false}
                    placeholder="#ff7a18"
                    title={t("settings.accent_hex")}
                    aria-label={t("settings.accent_hex")}
                    onChange={(e) => {
                      setCustomText(e.target.value);
                      // Only a WHOLE colour moves the swatch, so the field can be
                      // typed through "#ff7", "#ff7a18" without the picker
                      // jumping to a colour that was never meant.
                      const next = normalizeHex(e.target.value);
                      if (next) setCustomHex(next);
                    }}
                    className="input !w-28 !py-1 font-mono text-xs"
                  />
                  <button
                    className="btn !py-1 text-xs tap"
                    disabled={!typedHex}
                    onClick={() => typedHex && pickAccent(typedHex)}
                  >
                    {t("settings.accent_use")}
                  </button>
                  {customText.trim() !== "" && !typedHex && (
                    <span className="text-[10px] text-red-400">{t("settings.accent_invalid")}</span>
                  )}
                  {/* The presets are always one tap above; this is the way back
                      for a colour that REPLACED the preset that was in force. */}
                  {customActive && (
                    <button className="btn-ghost !py-1 text-xs tap" onClick={() => pickAccent(lastPreset.current)}>
                      {t("settings.accent_back")}
                    </button>
                  )}
                </div>
                <div className="text-[10px] text-zinc-600 mt-1">{t("settings.accent_help")}</div>
              </div>
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">Default library view</span>
                <select className="input mt-1" value={defaultView} onChange={(e) => pickDefaultView(e.target.value)}>
                  <option value="grid">Grid (cover browse)</option>
                  <option value="compact">Compact (grading status)</option>
                  <option value="albums">Albums table</option>
                  <option value="artists">Artists</option>
                  <option value="tracks">Tracks</option>
                </select>
              </label>

              <div className="pt-2 border-t border-border">
                <span className="text-xs text-zinc-500 uppercase">Player &amp; lyrics display</span>
                <div className="space-y-2.5 mt-2">
                  <label className="flex items-center justify-between gap-3 text-xs text-zinc-300">
                    <span>Fullscreen lyrics size</span>
                    <select
                      className="input !w-28 !py-1 tap"
                      value={localStorage.getItem("mlo.np.size") ?? "md"}
                      onChange={(e) => localStorage.setItem("mlo.np.size", e.target.value)}
                    >
                      <option value="sm">Small</option>
                      <option value="md">Medium</option>
                      <option value="lg">Large</option>
                    </select>
                  </label>
                  <label className="flex items-center justify-between gap-3 text-xs text-zinc-300 cursor-pointer">
                    <span>Karaoke word highlight (vs. whole-line)</span>
                    <input
                      type="checkbox"
                      defaultChecked={localStorage.getItem("mlo.np.karaoke") === "1"}
                      onChange={(e) => localStorage.setItem("mlo.np.karaoke", e.target.checked ? "1" : "0")}
                    />
                  </label>
                  <label className="flex items-center justify-between gap-3 text-xs text-zinc-300 cursor-pointer">
                    <span>Animated background in fullscreen player</span>
                    <input
                      type="checkbox"
                      defaultChecked={localStorage.getItem("mlo.np.orbs") !== "0"}
                      onChange={(e) => localStorage.setItem("mlo.np.orbs", e.target.checked ? "1" : "0")}
                    />
                  </label>
                  <label className="flex items-center justify-between gap-3 text-xs text-zinc-300">
                    <span>Default lyrics save target</span>
                    <select
                      className="input !w-40 !py-1 tap"
                      value={localStorage.getItem("mlo.lyricsSaveTarget") ?? "embedded"}
                      onChange={(e) => localStorage.setItem("mlo.lyricsSaveTarget", e.target.value)}
                    >
                      <option value="embedded">Embedded tag</option>
                      <option value="sidecar">.lrc sidecar</option>
                      <option value="both">Tag + .lrc</option>
                    </select>
                  </label>
                </div>
                <div className="text-[10px] text-zinc-600 mt-2">Stored per browser, like the accent color.</div>
              </div>
            </div>
          )}

          {tab === "naming" && (
            <div className="panel space-y-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">File naming (Picard-style script)</div>
              <textarea
                className="input font-mono text-xs min-h-[110px]"
                value={namingScript}
                onChange={(e) => setNamingScript(e.target.value)}
                spellCheck={false}
              />
              <div className="text-[11px] text-zinc-600 leading-relaxed">
                Variables: <code>%albumartist% %musicbrainz_albumartistid% %album% %musicbrainz_albumid% %title% %musicbrainz_trackid% %releasetype% %year% %originaldate% %date% %label% %releasecountry% %media% %catalognumber% %discnumber% %tracknumber%</code> ·
                Functions: <code>$if(a,b,c) $left(s,n) $right(s,n) $num(s,n) $lower $upper $replace $eq $ne $not $and $or</code> · <code>/</code> creates folders.
                Applied from the album page or the bulk selection toolbar; Grading compares every path against this script.
              </div>
              <div className="flex items-center gap-4 flex-wrap">
                <label className="flex items-center gap-2 text-xs text-zinc-400 cursor-pointer select-none">
                  <input type="checkbox" checked={shortFolderNames} onChange={(e) => setShortFolderNames(e.target.checked)} />
                  Shorter folder names (truncate MusicBrainz IDs to 8 chars)
                </label>
                <button
                  className="btn-ghost !py-1 text-xs tap"
                  onClick={() =>
                    setNamingScript(
                      String((configDefaults as Record<string, unknown> | undefined)?.naming_script ?? "")
                    )
                  }
                >
                  <RotateCcw className="h-3.5 w-3.5" /> Reset to default
                </button>
                <button className="btn-ghost !py-1 text-xs tap" onClick={runPreview} disabled={previewing}>
                  Preview
                </button>
              </div>
              {previewPath && (
                <div className="rounded-md border border-border bg-panel px-3 py-2 font-mono text-xs text-accent-soft break-all">
                  <span className="text-zinc-500">sample album → </span>
                  {previewPath}
                </div>
              )}
              {previewError && (
                <div className="rounded-md border border-red-900 bg-red-950/40 px-3 py-2 font-mono text-xs text-red-300 break-all">
                  {previewError}
                </div>
              )}
            </div>
          )}

          {tab === "sources" && (
            <div className="panel space-y-3">
              <SourcesPanel />
            </div>
          )}

          {tab === "deps" && (
            <div className="panel space-y-3">
              <div className="flex items-center justify-between flex-wrap gap-2">
                <div>
                  <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Dependencies</div>
                  <div className="text-[11px] text-zinc-600 mt-0.5">
                    Tools the scripts need. Installed from <code className="font-mono break-all">{deps?.deps_dir ?? ".dependencies"}</code> or found on PATH.
                  </div>
                </div>
                <div className="flex flex-wrap gap-2">
                  <button className="btn-ghost !py-1 text-xs tap" onClick={() => refetchDeps()} disabled={depsBusy}>
                    Refresh
                  </button>
                  {/* ONE page-level button, and the same words as the
                      Dependencies page: the press installs what is missing AND
                      takes the newest release for what is behind, so an update
                      waiting makes it "Update all". The counts are in the
                      title/aria-label — the label has no room for them. */}
                  <button
                    className="btn-primary !py-1 text-xs min-h-10 md:min-h-0"
                    onClick={() => installDeps()}
                    disabled={depsBusy || !!deviceReason || (depMissing.length === 0 && depUpdates.length === 0)}
                    title={
                      deviceReason ??
                      `Installs the ${depMissing.length} missing tool(s) and updates the ${depUpdates.length} behind; an installed tool takes the newest release upstream has, and one already at it is left alone`
                    }
                    aria-label={`${depUpdates.length ? "Update all" : "Install all"}: ${depMissing.length} missing, ${depUpdates.length} update(s)`}
                  >
                    {depsBusy && !depBusyKey ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                    {depsBusy && !depBusyKey ? "Installing…" : depUpdates.length ? "Update all" : "Install all"}
                  </button>
                </div>
              </div>
              {/* One honest line about what THIS build can do, from the
                  backend's own capability report. A tool that is missing on a
                  device that can run one is a download; a device that cannot
                  start a program at all is never fixed by one, and the table
                  below says which of the two it is looking at. */}
              {caps && deviceReason && (
                <div className="rounded-md border border-border bg-bg/60 px-3 py-2 text-[11px] text-zinc-400 leading-relaxed">
                  <span className="text-amber-400">Unavailable on this device</span> — {deviceReason}. Browsing,
                  tagging, playing, playlists and lyrics work here; the {unavailable.length} other feature
                  {unavailable.length === 1 ? "" : "s"} need a tool this device can start.
                </div>
              )}
              {/* `table-scroll` alone: the utility `overflow-hidden` used to sit
                  beside it and, living in the utilities layer, won — the table
                  was clipped instead of scrolled at a phone width. */}
              <div className="rounded-md border border-border table-scroll">
                <table className="w-full text-sm">
                  <thead className="bg-panel/60">
                    <tr>
                      <th className="th">Tool</th>
                      <th className="th">Status</th>
                      <th className="th">Installed</th>
                      <th className="th">Latest</th>
                      <th className="th" title="Newest release upstream has published. An installed tool takes exactly that one when you press Update — the reviewed pin in Latest is what a FIRST install fetches.">
                        Available
                      </th>
                      <th className="th">Path</th>
                      <th className="th">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {depTools.map((t) => (
                      <tr key={t.key} className="table-row cursor-default">
                        <td className="td font-medium">{t.name}</td>
                        <td className="td">
                          {t.state === "ok" && <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800">Ready</span>}
                          {t.state === "update" && (
                            <span
                              className="chip bg-amber-900/50 text-amber-300 border border-amber-900"
                              title={t.note ?? (t.upstream_version ? `Upstream: ${t.upstream_version}` : undefined)}
                            >
                              Update
                            </span>
                          )}
                          {t.state === "missing" && (
                            <span
                              className={`chip border ${deviceReason || t.installable === false
                                ? "bg-zinc-800 text-zinc-400 border-zinc-700"
                                : "bg-red-900/50 text-red-300 border-red-900"}`}
                              title={deviceReason ?? t.install_note ?? undefined}
                            >
                              {deviceReason
                                ? "Unavailable here"
                                : t.installable === false
                                  ? t.install_kind === "system" ? "System package" : "No build here"
                                  : "Missing"}
                            </span>
                          )}
                          {t.state === "error" && (
                            <span className="chip bg-zinc-800 text-zinc-400 border border-zinc-700" title={t.note ?? undefined}>
                              Check failed
                            </span>
                          )}
                        </td>
                        <td className="td text-zinc-500">{t.installed_version ?? t.detected_version ?? "—"}</td>
                        <td className="td text-zinc-500">{t.latest_version ?? "—"}</td>
                        <td className="td text-zinc-500" title={t.note ?? ""}>
                          {t.upstream_version ?? (deps?.checking ? "checking…" : "—")}
                        </td>
                        <td className="td text-zinc-500 truncate max-w-[280px]">{t.path ?? "—"}</td>
                        {/* The row's own Install/Update, mirroring the chip
                            beside it: a tool this device cannot install gets no
                            button, and the row's own note/install_note is the
                            hover text, so the button cannot promise more than
                            the row it belongs to. */}
                        <td className="td">
                          {(t.state === "missing" || t.state === "update") && (
                            <button
                              className="btn-ghost !py-0.5 text-[11px] tap"
                              onClick={() => installDeps([t.key], t.key)}
                              disabled={depsBusy || !!deviceReason || t.installable === false}
                              title={
                                deviceReason ??
                                (t.state === "update"
                                  ? t.note ?? (t.upstream_version ? `Upstream: ${t.upstream_version}` : undefined)
                                  : t.install_note ?? t.note ?? undefined)
                              }
                            >
                              {depBusyKey === t.key ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
                              {t.state === "update" ? "Update" : "Install"}
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="text-[10px] text-zinc-600">
                Install downloads from the tool's GitHub releases into the dependencies folder; PATH-installed
                tools (scoop etc.) are shown as ready. An INSTALLED tool takes the newest release{" "}
                <span className="text-zinc-500">Available</span> names, and a row already at that version is a
                no-op — nothing is downloaded. Only a FIRST install takes the reviewed pinned version in{" "}
                <span className="text-zinc-500">Latest</span>.
                {deps?.note && <span className="text-amber-500"> Upstream check: {deps.note}</span>}
              </div>
            </div>
          )}

          {GROUP_BY_TAB[tab] && (
            <div className="panel space-y-3">
              <div className="text-xs font-bold text-zinc-300">{GROUP_BY_TAB[tab].title}</div>
              {GROUP_BY_TAB[tab].blurb && <div className="text-[10px] text-zinc-600">{GROUP_BY_TAB[tab].blurb}</div>}
              {renderFields(GROUP_BY_TAB[tab].fields)}
              {/* The on-device playback report belongs beside the playback
                  settings it explains: this is the block the owner opens on the
                  phone that misbehaves (lib/pbDiag holds the black box, and the
                  panel is deliberately collapsible so Settings stays readable
                  for everyone else). */}
              {tab === "downloads" && <PlaybackDiag />}
              {tab === "ai" && (
                <div className="pt-2 border-t border-border space-y-1">
                  <AiTestButton value={scriptCfg} />
                  <div className="text-[10px] text-zinc-600">
                    Tests the values on screen — save first if you want them to stick. Script 17 runs from the
                    Optimization page, the library selection menu, or as part of Run All.
                  </div>
                </div>
              )}
              {tab === "images" && (
                <div className="pt-2 border-t border-border">
                  <CoverDefaults />
                </div>
              )}
              {tab === "videos" && <YoutubeCookieJar />}
              {tab === "discovery" && <RymCookieJar onStored={(v) => setCfg("rym_cookie", v)} />}
              {tab === "beets" && (
                <div className="pt-2 border-t border-border space-y-2">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-xs text-zinc-400">
                      {beetsStatus?.installed
                        ? `beets v${beetsStatus.version} installed (vendored in .dependencies)`
                        : "beets is not installed yet"}
                    </span>
                    <button className="btn-primary !py-1 text-xs min-h-10 md:min-h-0" onClick={installBeets} disabled={beetsBusy}>
                      {beetsBusy ? "Installing…" : beetsStatus?.installed ? "Reinstall" : "Install beets"}
                    </button>
                  </div>
                  {beetsStatus?.installed && (
                    <details className="text-[11px]">
                      <summary className="cursor-pointer text-zinc-500">generated beets config ({"<music folder>/.mlo/data/beets-config.yaml"})</summary>
                      <pre className="mt-1 p-2 bg-zinc-950 border border-border rounded overflow-auto max-h-64 text-[10px] font-mono text-zinc-400">{beetsStatus.config}</pre>
                    </details>
                  )}
                  <div className="text-[10px] text-zinc-600">
                    Run "Tag with beets" from an album page to import it: beets matches against MusicBrainz, writes tags
                    (translations / work &amp; movement / release-type caps per the settings above) and organizes files with
                    your naming script.
                  </div>
                </div>
              )}
              {tab === "import" && (
                <div className="pt-2 border-t border-border space-y-1">
                  <div className="text-[11px] text-zinc-400">
                    Chain after saving ({chainPreview?.count ?? 0}{" "}
                    {(chainPreview?.count ?? 0) === 1 ? "script" : "scripts"}):{" "}
                    <span className="text-zinc-300">
                      {chainPreview?.chain.length
                        ? chainPreview.chain
                            .map((id) => chainPreview.labels[String(id)] ?? `#${id}`)
                            .join(" → ")
                        : "nothing runs"}
                    </span>
                  </div>
                  <div className="text-[10px] text-zinc-600">
                    Read from the saved config — click <em>Save all settings</em> to preview an edited list.
                    A blank field falls back to the built-in chain, and the chain is skipped entirely when
                    &ldquo;Run the script chain after import&rdquo; is off.
                  </div>
                </div>
              )}
              {tab === "tagwrites" && (
                <>
                  <div className="pt-2 border-t border-border">
                    <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Per-filetype tag writes</div>
                    <div className="text-[10px] text-zinc-600 mt-0.5 mb-1.5">
                      Which tag families each audio container receives (ANDed with the global switches above).
                    </div>
                    <div className="table-scroll">
                      <table className="w-full text-xs">
                        <thead>
                          <tr>
                            <th className="text-left text-zinc-500 font-medium py-1">Type</th>
                            {TAG_FAMILIES.map((fam) => (
                              <th key={fam} className="text-zinc-500 font-medium py-1">{fam}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {AUDIO_TYPES.map((t) => (
                            <tr key={t}>
                              <td className="py-0.5 text-zinc-300">{t}</td>
                              {TAG_FAMILIES.map((fam) => (
                                <td key={fam} className="py-0.5">
                                  <input
                                    type="checkbox"
                                    className="accent-[var(--accent)]"
                                    checked={!!audioTagWrites[t]?.[fam]}
                                    onChange={(e) => toggleTagWrite(t, fam, e.target.checked)}
                                  />
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                  <div className="pt-2 border-t border-border">
                    <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Encoder marker tags</div>
                    <div className="text-[10px] text-zinc-600 mt-0.5 mb-1.5">
                      Written to files when re-encoded. QUALITY/VERSION gate re-optimization; PROGRAM is informational.
                    </div>
                    <div className="table-scroll">
                      <table className="w-full text-xs">
                        <thead>
                          <tr>
                            <th className="text-left text-zinc-500 font-medium py-1">Format</th>
                            {ENCODER_FIELDS.map((f) => (
                              <th key={f} className="text-zinc-500 font-medium py-1">{f}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {ENCODER_FORMATS.map((fmt) => (
                            <tr key={fmt}>
                              <td className="py-0.5 text-zinc-300">{fmt}</td>
                              {ENCODER_FIELDS.map((f) => (
                                <td key={f} className="py-0.5">
                                  <input
                                    type="checkbox"
                                    className="accent-[var(--accent)]"
                                    checked={!!encoderTags[fmt]?.[f]}
                                    onChange={(e) => toggleEncoder(fmt, f, e.target.checked)}
                                  />
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </>
              )}
            </div>
          )}

          {tab === "grading" && (
            <details className="panel" open>
              <summary className="text-sm font-semibold cursor-pointer">Individual grading checks ({GRADE_CHECK_KEYS.length})</summary>
              <div className="mt-2">{renderFields(GRADE_CHECK_KEYS)}</div>
            </details>
          )}

          {tab === "security" && <SecurityPanel />}

          {tab === "notifications" && (
            <div className="panel space-y-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
                {t("settings.notifications")}
              </div>
              <p className="text-[11px] text-zinc-600 leading-relaxed">{t("settings.notifications_help")}</p>
              <div className="flex flex-wrap items-center gap-2">
                <button
                  className="btn-ghost !py-1.5 text-xs tap"
                  onClick={async () => {
                    const state = await requestNotifications();
                    setNotifyState(state);
                    if (state === "granted") toast.success(t("notify.enabled"));
                    else if (state === "denied") toast.error(t("notify.blocked_help"));
                  }}
                >
                  <Bell className="h-3.5 w-3.5" /> {t("notify.enable")}
                </button>
                <span className="text-[11px] text-zinc-500">
                  {notifyState === "granted"
                    ? t("notify.title_granted")
                    : notifyState === "denied"
                    ? t("notify.blocked")
                    : t("notify.enable")}
                </span>
              </div>
              <div className="flex flex-wrap gap-x-6 gap-y-1.5">
                {NOTIFY_KEYS.map((f) => (
                  <label key={f.k} className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                    <input type="checkbox" checked={!!scriptCfg[f.k]} onChange={(e) => setCfg(f.k, e.target.checked)} />
                    {f.label}
                  </label>
                ))}
              </div>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <ConfirmButton
              onConfirm={resetAllDefaults}
              confirmLabel="Reset all"
              disabled={!configDefaults}
              title="Restore factory defaults for every setting (music folder and first-run flag are kept)"
            >
              <RotateCcw className="h-4 w-4" /> Reset to defaults
            </ConfirmButton>
            <ConfirmButton
              onConfirm={resetUiLayout}
              confirmLabel="Reset layout"
              title="Clear this browser's UI preferences — accent, sidebar, grid sizes, column layouts and widths, custom columns, viewer options — and reload"
            >
              <LayoutGrid className="h-4 w-4" /> Reset UI & layout
            </ConfirmButton>
            <button
              className="btn-primary min-h-10 md:min-h-0"
              onClick={save}
              // The form is seeded FROM the config; saving before it loaded
              // posts empty strings over live values (an empty music_folder
              // survives the server's merge and costs the library root).
              disabled={!loaded}
              title={loaded ? "Write these settings to the config" : "Waiting for the current configuration to load"}
            >
              <Save className="h-4 w-4" /> Save all settings
            </button>
            {(configError || !loaded) && (
              <span className="text-xs text-amber-400/90 self-center">
                {configError
                  ? "Could not load the configuration — saving is disabled so a failed load cannot overwrite it. Retry by reloading the page."
                  : "Loading the current configuration…"}
              </span>
            )}
          </div>

          <details className="panel">
            <summary className="text-sm font-semibold cursor-pointer">Credits & open-source licenses</summary>
            <p className="text-[11px] text-zinc-500 mt-2">
              la musica is MIT-licensed (see LICENSE) and stands on the shoulders
              of these projects — their licenses require this credit, and they
              deserve it. The full legal text lives in THIRD-PARTY-NOTICES.md.
            </p>
            <div className="mt-2 space-y-3">
              {((credits as { groups?: { title: string; items: { name: string; license: string; url: string }[] }[] } | undefined)?.groups ?? []).map((g) => (
                <div key={g.title}>
                  <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-1">{g.title}</div>
                  <div className="grid gap-x-4 gap-y-0.5" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))" }}>
                    {g.items.map((c: { name: string; license: string; url: string }) => (
                      <a
                        key={c.name}
                        href={c.url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-[11px] text-zinc-400 hover:text-accent-soft truncate"
                        title={`${c.name} — ${c.license}`}
                      >
                        <span className="text-zinc-200">{c.name}</span>
                        <span className="text-zinc-600"> · {c.license}</span>
                      </a>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </details>

          <details className="panel">
            <summary className="text-sm font-semibold cursor-pointer">Raw config (advanced)</summary>
            <textarea
              className="input font-mono text-[11px] min-h-[220px] mt-2"
              value={rawConfig}
              onChange={(e) => setRawConfig(e.target.value)}
              spellCheck={false}
            />
            <div className="flex items-center gap-2 mt-2">
              <button className="btn-ghost !py-1 text-xs tap" onClick={applyRaw}>
                Apply to form
              </button>
              <span className="text-[10px] text-zinc-600">
                Edits the form fields (then click Save all settings). Invalid JSON is rejected.
              </span>
            </div>
          </details>
        </div>
      </div>
      {picker && (
        <FolderPicker
          startPath={musicFolder}
          onClose={() => setPicker(false)}
          onPicked={(chosen) => {
            setMusicFolder(chosen);
            setPicker(false);
          }}
        />
      )}
    </div>
  );
}
