import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  Eye,
  EyeOff,
  FolderOpen,
  KeyRound,
  Loader2,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  Users,
  X,
} from "lucide-react";
import { api, deviceUnavailable, installSummary, setToken, unavailableFeatures } from "../api";
import FolderPicker from "../components/FolderPicker";
import SourcesPanel from "../components/SourcesPanel";
import AiTestButton from "../components/AiTestButton";
import { toast } from "../store";
import { LOCALES } from "../lib/i18n";
import { DEFAULT_RUN_ALL, SCRIPT_LABEL, isScriptId } from "../lib/scripts";
import {
  OPEN_GROUPS,
  SETUP_STEPS,
  cfgChanges,
  cfgDraft,
  cfgError,
  cfgGroup,
  stepFields,
  type CfgField,
} from "../lib/configMeta";

/** Every field of every step, so one draft holds the whole wizard: a step's
 *  save sends only the keys THAT step changed (see cfgChanges), and the closing
 *  screen can say how far from the shipped defaults the answers land. */
const ALL_FIELDS = SETUP_STEPS.flatMap(stepFields);

/** Genre sources that never answer for a single track — the chain files their
 *  answer under every track and marks it `level: "album"`/`"artist"`
 *  (server/integrations._genre_source_answers). Everything absent here is asked
 *  per track first, so "per track" is what the picker says for them. */
const GENRE_LEVEL: Record<string, string> = {
  rateyourmusic: "album, per track when its release page states one",
  discogs: "album only",
  bandcamp: "album only",
  deezer: "album only",
  spotify: "artist only",
};

type ProviderOption = { id: string; label: string; notes?: string; rank?: number };

export default function SetupPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const { data: defaults } = useQuery({ queryKey: ["configDefaults"], queryFn: api.configDefaults, staleTime: Infinity });
  const [index, setIndex] = useState(0);
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null);
  const [showPasswords, setShowPasswords] = useState<Record<string, true>>({});
  const [musicFolder, setMusicFolder] = useState("");
  // The folder step's picker; the dialog saves the choice as it closes.
  const [picker, setPicker] = useState(false);
  const [busy, setBusy] = useState(false);
  // The dependency row whose own Install/Update press is in flight. The
  // page-level button settles the table through `busy` + `refetchDeps()` (the
  // same mechanism a row press uses), and this only decides which row spins.
  const [busyDep, setBusyDep] = useState<string | null>(null);
  // The password form (panel "password"). Kept apart from the draft because it
  // is not a config key: the server hashes it through /api/auth/*, never
  // through /api/config.
  const [currentPw, setCurrentPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");

  const step = SETUP_STEPS[index];

  const { data: deps, refetch: refetchDeps } = useQuery({
    queryKey: ["dependencies"],
    queryFn: () => api.dependencies(),
    retry: false,
    enabled: step.panel === "dependencies",
    // The upstream (GitHub) check runs in the backend's background thread and
    // the first answer therefore says "checking…". Without this the wizard
    // fetched ONCE, and every row kept that placeholder for as long as the page
    // stayed open — so a tool with an update waiting looked Ready, the Install
    // button looked like it had nothing to do, and the answer the server had
    // already cached was never read. Same rule, and the same reason, as the
    // Dependencies page: ask again while a check is in flight, then stop.
    refetchInterval: (query) => (query.state.data?.checking ? 5000 : false),
  });

  // What THIS build can do, so the wizard never offers to install a tool the
  // device could not run even if the download succeeded (see api.deviceUnavailable).
  const { data: caps } = useQuery({
    queryKey: ["capabilities"],
    queryFn: () => api.capabilities(),
    retry: false,
    enabled: step.panel === "dependencies",
    staleTime: 5 * 60 * 1000,
  });
  const deviceReason = deviceUnavailable(caps);
  // What the page-level button's count and label are about, and only what this
  // host can actually fetch: a Windows-only tool on Linux, or one the image
  // already provides as a distro package, has nothing to download (the row's
  // `installable`), so counting it would make the button promise work it can
  // never do.
  const missing = (deps?.tools ?? []).filter((t) => t.state === "missing" && t.installable !== false);
  // Rows behind upstream, the same rule as `missing`: a tool this host cannot
  // install is not work the button can do. The page-level press takes these
  // too (the server installs an installed copy's NEWEST release, not the pin),
  // so the button's label has to say so — the wizard's step is where the
  // screenshot for this was taken.
  const updates = (deps?.tools ?? []).filter((t) => t.state === "update" && t.installable !== false);

  // Catalogues the pickers draw their options from. Fetched only once the
  // steps that need them are reachable, so the folder step costs one request.
  const live = index > 0;
  const { data: discoveryCat } = useQuery({ queryKey: ["discoverySources"], queryFn: api.discoverySources, enabled: live });
  const { data: lyricsCat } = useQuery({ queryKey: ["lyricsProviders"], queryFn: api.lyricsProviders, enabled: live });
  const { data: coverCat } = useQuery({ queryKey: ["coverSources"], queryFn: api.coverSources, enabled: live });
  // The genre rows of the health report: the same list the genre picker later
  // offers in Settings, so a tick here cannot name a source the chain dropped.
  const { data: health } = useQuery({
    queryKey: ["sourcesHealth"],
    queryFn: () => api.sourcesHealth(false),
    enabled: live,
    staleTime: 30000,
  });
  const genreOptions: [string, string][] = (health?.sources ?? [])
    .filter((s) => s.kind === "genre")
    .map((s) => [s.id, `${s.label} — ${GENRE_LEVEL[s.id] ?? "per track"}`]);

  const { data: auth } = useQuery({
    queryKey: ["auth", "status"],
    queryFn: api.authStatus,
    enabled: step.panel === "password",
    retry: false,
  });

  // Seeded — and RE-seeded — from the saved config: the Sources panel on the
  // step before this one writes keys of its own (the RYM cookie, the provider
  // orders) through its own endpoint, and a draft seeded once would send those
  // stale values back as changes on the next save.
  //
  // The re-seed is per key: a control the user has since typed in keeps what
  // was typed, and one still showing the value that just changed underneath it
  // adopts the new one. Re-seeding wholesale would throw away answers entered
  // on a step the user stepped away from — silent loss of their typing.
  const seeded = useRef<Record<string, unknown> | null>(null);
  useEffect(() => {
    if (!config) return;
    const fresh = cfgDraft(config, ALL_FIELDS);
    const previous = seeded.current;
    seeded.current = fresh;
    setDraft((current) => {
      if (!current || !previous) return fresh;
      const next = { ...current };
      for (const [k, v] of Object.entries(fresh)) {
        const typed = JSON.stringify(current[k]) !== JSON.stringify(previous[k]);
        const moved = JSON.stringify(previous[k]) !== JSON.stringify(v);
        if (moved && !typed) next[k] = v;
      }
      return next;
    });
    setMusicFolder(String(config.music_folder ?? ""));
  }, [config]);

  // Validation runs against the step on screen only: a field the user has not
  // reached yet cannot block a save, and the message sits under the control
  // that has to change.
  const stepErrors: Record<string, string> = {};
  for (const field of stepFields(step)) {
    const message = cfgError(field, draft?.[field.k]);
    if (message) stepErrors[field.k] = message;
  }
  const badCount = Object.keys(stepErrors).length;

  const setField = (k: string, value: unknown) => setDraft((d) => (d ? { ...d, [k]: value } : d));

  const listCatalog: Record<string, { options: ProviderOption[]; builtin: (k: string) => string[] }> = {
    discovery: { options: discoveryCat?.sources ?? [], builtin: (k) => discoveryCat?.defaults?.[k] ?? [] },
    // Only time-synced providers are pickable: a plain-lyrics-only entry has no
    // place in the chain (the `synced` flag comes from the endpoint).
    lyrics: {
      options: (lyricsCat?.sources ?? []).filter((s) => s.synced !== false),
      builtin: () => lyricsCat?.default_order ?? [],
    },
    covers: {
      options: (coverCat?.sources ?? []).map((s) => ({ id: s.id, label: s.name })),
      builtin: () => coverCat?.default_sources ?? [],
    },
  };

  /** A value-picker's options: the field's own constants, or the catalogue it
   *  names — a value list the module cannot carry (the app's locales, the
   *  cover regions, the genre sources). */
  const optionsFor = (field: CfgField): [string, string][] => {
    if (field.type === "multi") return field.optionsFrom === "genres" ? genreOptions : field.options;
    if (field.type !== "select") return [];
    if (field.optionsFrom === "locales") return LOCALES.map((l) => [l.code, l.label] as [string, string]);
    if (field.optionsFrom === "coverCountries") return (coverCat?.countries ?? []).map((c) => [c, c.toUpperCase()] as [string, string]);
    return field.options;
  };

  /** `keys` undefined = everything missing or behind (the page button);
   *  `keys` = [one tool] from that row's own button. `pressed` is the row that
   *  asked, so it carries the spinner — the settling is the same either way:
   *  this `busy` flag plus `refetchDeps()`, never a state the server has not
   *  answered yet. */
  const installDeps = async (keys?: string[], pressed?: string) => {
    setBusy(true);
    setBusyDep(pressed ?? null);
    try {
      const r = await api.installDependencies(keys);
      const summary = installSummary(r.results);
      if (r.results.some((x) => !x.ok)) toast.error(summary);
      else toast.success(summary);
      refetchDeps();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
      setBusyDep(null);
    }
  };

  /** Hand a distro row's own upgrade command to the clipboard. The command is
   *  the backend's (never spelled out here) and is copied, not run: this app
   *  does not drive a package manager. Same wording as the Dependencies page,
   *  which offers the same button for the same rows. */
  const copyCommand = async (cmd: string) => {
    try {
      await navigator.clipboard.writeText(cmd);
      toast.success(`Copied: ${cmd}`);
    } catch {
      toast.error(`Could not reach the clipboard — run it yourself: ${cmd}`);
    }
  };

  /** Save this step's answers and move on. `advance` false = stay, which is
   *  only used by the step that also starts slskd. */
  const saveStep = async (advance: boolean) => {
    if (!draft || badCount) return;
    const changed = cfgChanges(draft, config ?? {}, stepFields(step));
    setBusy(true);
    try {
      if (Object.keys(changed).length) await api.saveConfig(changed);
      if (step.panel === "slskd") {
        if (draft.soulseek_share_library !== false) {
          await api.soulseekSharesRefresh().catch(() => undefined);
          await api.soulseekStart();
          toast("slskd started — sharing your library");
        } else if (Object.keys(changed).length) {
          toast.success("Saved");
        }
      } else if (Object.keys(changed).length) {
        toast.success("Saved");
      }
      qc.invalidateQueries({ queryKey: ["config"] });
      if (advance) setIndex((i) => i + 1);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  /** The wizard is over: nothing but the flag is written, so every step that
   *  was skipped keeps exactly what it had. */
  const exit = async (to: string) => {
    setBusy(true);
    try {
      await api.saveConfig({ first_run_done: true });
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["library"] });
      navigate(to, { replace: true });
    } catch (e) {
      toast.error(String(e));
      setBusy(false);
    }
  };

  const savePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy || !newPw) return;
    setBusy(true);
    try {
      // Setting the first password claims the server and hands this client a
      // session; changing one re-issues the caller's own session. Keep it, or
      // the tab signs itself out.
      const session = auth?.has_password
        ? await api.authChangePassword(currentPw, newPw, confirmPw)
        : await api.authSetup(newPw, confirmPw, String(draft?.auth_username ?? "").trim() || undefined);
      setToken(session.token);
      setCurrentPw("");
      setNewPw("");
      setConfirmPw("");
      toast.success(auth?.has_password ? "Password changed" : "Password set — every client will be asked to sign in");
    } catch (err) {
      // The server's own words: "wrong password", "at least 8 characters".
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const renderField = (field: CfgField) => {
    const value = draft?.[field.k];
    const error = stepErrors[field.k];
    const set = (v: unknown) => setField(field.k, v);
    const help = field.help ? <span className="block text-[10px] text-zinc-600 mt-1 leading-relaxed">{field.help}</span> : null;
    const errorLine = error ? <span className="block text-[10px] text-amber-300 mt-1">{error}</span> : null;

    if (field.type === "bool") {
      return (
        <label key={field.k} className="flex items-start gap-2 text-xs text-zinc-300 cursor-pointer select-none">
          <input type="checkbox" className="mt-0.5 accent-[var(--accent)]" checked={!!value} onChange={(e) => set(e.target.checked)} />
          <span>
            {field.label}
            {help}
          </span>
        </label>
      );
    }

    if (field.type === "chain") {
      const ids = (Array.isArray(value) ? value.map(Number).filter(isScriptId) : []) as number[];
      const order = ids.length ? ids : DEFAULT_RUN_ALL;
      // A re-ticked script goes back to its factory position instead of
      // jumping to the end — the same rule the Settings page's Run All grid
      // applies, so the two cannot disagree about what "the chain" is.
      const toggle = (id: number, on: boolean) =>
        set(
          on
            ? [...order.filter((x) => DEFAULT_RUN_ALL.indexOf(x) < DEFAULT_RUN_ALL.indexOf(id)), id, ...order.filter((x) => DEFAULT_RUN_ALL.indexOf(x) >= DEFAULT_RUN_ALL.indexOf(id))]
            : order.filter((x) => x !== id),
        );
      return (
        <div key={field.k} className="sm:col-span-2">
          <span className="text-xs text-zinc-500 uppercase">{field.label}</span>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1.5 mt-1">
            {[...order, ...DEFAULT_RUN_ALL.filter((id) => !order.includes(id))].map((id) => (
              <label key={id} className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                <input type="checkbox" className="accent-[var(--accent)]" checked={order.includes(id)} onChange={(e) => toggle(id, e.target.checked)} />
                <span className="text-zinc-600 w-4">{id}</span>
                {SCRIPT_LABEL[id] ?? `#${id}`}
              </label>
            ))}
          </div>
          {help}
        </div>
      );
    }

    if (field.type === "matrix") {
      const cells = (value ?? {}) as Record<string, Record<string, boolean>>;
      return (
        <div key={field.k} className="sm:col-span-2">
          <span className="text-xs text-zinc-500 uppercase">{field.label}</span>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-1">
            {field.cols.map(([col, colLabel]) => (
              <div key={col} className="rounded-md border border-border bg-bg/60 px-3 py-2">
                <div className="text-[10px] uppercase tracking-wider text-zinc-500">{colLabel}</div>
                <div className="flex flex-wrap gap-x-3 gap-y-1 mt-1">
                  {field.rows.map(([row, rowLabel]) => (
                    <label key={row} className="flex items-center gap-1.5 text-[11px] text-zinc-300 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        className="accent-[var(--accent)]"
                        checked={!!cells[col]?.[row]}
                        onChange={(e) => set({ ...cells, [col]: { ...(cells[col] ?? {}), [row]: e.target.checked } })}
                      />
                      {rowLabel}
                    </label>
                  ))}
                </div>
              </div>
            ))}
          </div>
          {help}
        </div>
      );
    }

    if (field.type === "list") {
      const catalog = listCatalog[field.catalog];
      const order = Array.isArray(value) ? (value as unknown[]).map(String) : [];
      // An empty list means the built-in order, shown rather than written: the
      // moment the user moves or drops one, the whole order becomes explicit —
      // which is exactly what the config value then means.
      const shown = order.length ? order : catalog.builtin(field.k).map(String);
      const move = (i: number, delta: number) => {
        const next = shown.slice();
        [next[i], next[i + delta]] = [next[i + delta], next[i]];
        set(next);
      };
      return (
        <div key={field.k} className="sm:col-span-2">
          <span className="text-xs text-zinc-500 uppercase">{field.label}</span>
          <div className="mt-1 space-y-1.5">
            <div className="rounded-md border border-border divide-y divide-border/60">
              {shown.map((id, i) => {
                const option = catalog.options.find((o) => o.id === id);
                return (
                  <div key={id} className="flex items-center gap-2 px-2 py-1">
                    <span className="w-4 text-[10px] text-zinc-600">{i + 1}</span>
                    <span className="flex-1 min-w-0 text-[12px] text-zinc-200 truncate">{option?.label ?? id}</span>
                    <button type="button" className="p-1 text-zinc-500 hover:text-white disabled:opacity-30" title="Move up" disabled={i === 0} onClick={() => move(i, -1)}>
                      <ChevronUp className="h-3.5 w-3.5" />
                    </button>
                    <button type="button" className="p-1 text-zinc-500 hover:text-white disabled:opacity-30" title="Move down" disabled={i === shown.length - 1} onClick={() => move(i, 1)}>
                      <ChevronDown className="h-3.5 w-3.5" />
                    </button>
                    <button type="button" className="p-1 text-zinc-500 hover:text-red-300" title="Drop from the list (an unlisted provider is never used)" onClick={() => set(shown.filter((x) => x !== id))}>
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                );
              })}
              {!shown.length && <div className="px-2 py-1 text-[11px] text-zinc-500">Every provider, in the built-in order.</div>}
            </div>
            {catalog.options.some((o) => !shown.includes(o.id)) && (
              <div className="flex flex-wrap gap-1.5">
                {catalog.options
                  .filter((o) => !shown.includes(o.id))
                  .map((o) => (
                    <button key={o.id} type="button" className="chip border border-white/15 bg-white/5 text-[10px] text-zinc-400 hover:text-white tap" title={o.notes} onClick={() => set([...shown, o.id])}>
                      + {o.label}
                    </button>
                  ))}
              </div>
            )}
            {order.length > 0 && (
              <button type="button" className="text-[10px] text-zinc-500 hover:text-white underline tap" onClick={() => set([])}>
                Reset to the built-in order
              </button>
            )}
          </div>
          {help}
        </div>
      );
    }

    if (field.type === "multi") {
      const options = optionsFor(field);
      const on = Array.isArray(value) ? (value as unknown[]).map(String) : [];
      // An id the catalogue does not list (the health payload has not arrived)
      // stays visible, or a saved value would silently disappear from the list.
      const shown: [string, string][] = [...options, ...on.filter((v) => !options.some(([o]) => o === v)).map((v): [string, string] => [v, v])];
      return (
        <div key={field.k} className="sm:col-span-2">
          <span className="text-xs text-zinc-500 uppercase">{field.label}</span>
          <div className="flex flex-wrap gap-x-4 gap-y-1 mt-1">
            {shown.map(([v, l]) => (
              <label key={v} className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                <input type="checkbox" className="accent-[var(--accent)]" checked={on.includes(v)} onChange={(e) => set(e.target.checked ? [...on, v] : on.filter((x) => x !== v))} />
                {l}
              </label>
            ))}
          </div>
          {help}
        </div>
      );
    }

    if (field.type === "csv") {
      const parts = Array.isArray(value) ? (value as unknown[]).map(String) : [];
      return (
        <label key={field.k} className="block sm:col-span-2">
          <span className="text-xs text-zinc-500 uppercase">{field.label}</span>
          <input className="input mt-1" value={parts.join(", ")} onChange={(e) => set(e.target.value.split(",").map((s) => s.trim()))} />
          {help}
        </label>
      );
    }

    if (field.type === "select") {
      // A value the option list does not carry (a catalogue that has not
      // arrived, or a region a newer backend added) still has to be visible, or
      // the control would look unset while the config holds a real value.
      const options = optionsFor(field);
      const current = String(value ?? "");
      const shown: [string, string][] = !current || options.some(([v]) => v === current) ? options : [[current, current], ...options];
      return (
        <label key={field.k} className="block">
          <span className="text-xs text-zinc-500 uppercase">{field.label}</span>
          <select className="input mt-1" value={current} onChange={(e) => set(e.target.value)}>
            {shown.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
          {help}
          {errorLine}
        </label>
      );
    }

    // text / password / number share one control shape: the wizard's own
    // label-over-input, with the value the config already holds.
    const isPassword = field.type === "password";
    return (
      <label key={field.k} className="block">
        <span className="text-xs text-zinc-500 uppercase">{field.label}</span>
        <div className={isPassword ? "relative mt-1" : undefined}>
          <input
            className={`input${isPassword ? " !pr-9" : ""}`}
            type={isPassword && !showPasswords[field.k] ? "password" : field.type === "number" ? "number" : "text"}
            min={field.type === "number" ? field.min : undefined}
            max={field.type === "number" ? field.max : undefined}
            step={field.type === "number" ? field.step : undefined}
            value={String(value ?? "")}
            onChange={(e) => set(e.target.value)}
            spellCheck={false}
          />
          {isPassword && (
            <button
              type="button"
              className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-zinc-500 hover:text-zinc-200"
              title={showPasswords[field.k] ? "Hide" : "Show"}
              onClick={() => setShowPasswords((prev) => (prev[field.k] ? { ...prev } : { ...prev, [field.k]: true }))}
            >
              {showPasswords[field.k] ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
            </button>
          )}
        </div>
        {help}
        {errorLine}
      </label>
    );
  };

  if (!draft) {
    // Held until the config answers: the draft is the saved config, and a form
    // rendered from nothing would read every untouched switch as a change.
    return (
      <div className="min-h-dvh bg-bg text-zinc-100 flex items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-zinc-500" />
      </div>
    );
  }

  // How far the answers land from the shipped defaults — the closing screen's
  // one honest number, computed from what the wizard would write.
  const pending = cfgChanges(draft, config ?? {}, ALL_FIELDS);
  const effective = { ...(config ?? {}), ...pending };
  const offDefaults = defaults ? Object.keys(cfgChanges(effective, defaults, ALL_FIELDS)).length : 0;
  const isLast = index === SETUP_STEPS.length - 1;

  return (
    <div className="min-h-dvh bg-bg text-zinc-100 flex flex-col items-center justify-center p-6">
      <div className="w-full max-w-2xl">
        <div className="flex items-center gap-2 mb-6">
          <img
            src="/icon.png"
            alt="la musica"
            className="h-9 w-9 rounded-md object-cover ring-1 ring-border shadow-sm"
          />
          <div className="flex-1">
            <div className="font-bold tracking-wide">la musica</div>
            <div className="text-xs text-zinc-500">{config?.first_run_done ? "Setup" : "First-run setup"}</div>
          </div>
          {/* The way out that is not a step: the wizard is re-runnable from
              Settings, and a first run with no library to point at yet should
              not have to walk twelve screens to leave. */}
          {!isLast && (
            <button className="btn-ghost !py-1 text-xs tap" disabled={busy} onClick={() => exit("/")}>
              Skip setup
            </button>
          )}
        </div>

        {/* The rail — the same idiom as the client wizard's. Twelve labelled
            steps wrap (six already needed it: see ClientSetup.tsx), so the
            labels are single words and every chip jumps to its step. */}
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-zinc-500 mb-4">
          {SETUP_STEPS.map((s, i) => (
            <button key={s.label} type="button" className="flex items-center gap-2 tap" disabled={busy} onClick={() => setIndex(i)}>
              <span
                className={`h-5 w-5 rounded-sm flex items-center justify-center text-[10px] border ${
                  i === index
                    ? "bg-accent text-[var(--accent-fg)] border-accent"
                    : i < index
                      ? "bg-emerald-900/60 text-emerald-300 border-emerald-800"
                      : "bg-panel border-border text-zinc-500"
                }`}
              >
                {i < index ? <Check className="h-3 w-3" /> : i + 1}
              </span>
              <span className={i === index ? "text-zinc-200" : "text-zinc-600"}>{s.label}</span>
            </button>
          ))}
        </div>

        <div className="panel p-6 space-y-4">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold">
              {step.panel === "sources" ? <Sparkles className="h-4 w-4 text-accent" /> : null}
              {step.panel === "slskd" ? <Users className="h-4 w-4 text-accent" /> : null}
              {step.panel === "password" ? <ShieldCheck className="h-4 w-4 text-accent" /> : null}
              {step.panel === "done" ? <Check className="h-4 w-4 text-emerald-400" /> : null}
              {step.title}
            </div>
            <p className="text-xs text-zinc-400 mt-1 leading-relaxed">{step.blurb}</p>
          </div>

          {step.panel === "folder" && (
            <>
              <div className="rounded-md border border-border bg-bg/60 px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="text-xs text-zinc-500 uppercase">Music folder</div>
                  <button className="btn-ghost !py-1 text-xs tap" onClick={() => setPicker(true)} disabled={busy}>
                    <FolderOpen className="h-3 w-3" /> Choose folder…
                  </button>
                </div>
                <div className="font-mono text-xs text-zinc-200 break-all mt-1">
                  {musicFolder.trim() || "not configured yet"}
                </div>
                <div className="text-[11px] text-zinc-500 mt-1 leading-relaxed">
                  The picker browses this machine's folders and saves the choice at once — its state (
                  <code className="font-mono">&lt;music&gt;/.mlo</code>) moves with it.{" "}
                  <code className="font-mono">MLO_MUSIC_FOLDER</code> still seeds it for a first start (Docker / compose).
                </div>
              </div>
            </>
          )}

          {step.panel === "dependencies" && (
            <>
              <div className="flex flex-wrap items-center justify-end gap-2">
                <button className="btn-ghost !py-1 text-xs" onClick={() => refetchDeps()} disabled={busy}>
                  <RotateCcw className="h-3 w-3" /> Refresh
                </button>
                {/* ONE page-level button: it installs the missing tools and
                    updates the rows behind upstream on the same press (the
                    server's key-less request takes both), so its label says
                    which of the two is the bulk of the work waiting. The
                    counts live in the title/aria-label, where a phone-width
                    button label cannot carry them. */}
                <button
                  className="btn-primary !py-1 text-xs"
                  onClick={() => installDeps()}
                  disabled={busy || !!deviceReason || (missing.length === 0 && updates.length === 0)}
                  title={
                    deviceReason ??
                    `Installs the ${missing.length} missing tool(s) and updates the ${updates.length} behind; an installed tool takes the newest release upstream has, and one already at it is left alone`
                  }
                  aria-label={`${updates.length ? "Update all" : "Install all"}: ${missing.length} missing, ${updates.length} update(s)`}
                >
                  {busy && !busyDep ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                  {busy && !busyDep ? "Installing…" : updates.length ? "Update all" : "Install all"}
                </button>
              </div>
              {/* One honest line per surface: what this build can and cannot do,
                  before anything is asked of it. The reasons per tool are on
                  the Dependencies page. */}
              {caps && deviceReason && (
                <div className="rounded-md border border-border bg-bg/60 px-3 py-2 text-[11px] text-zinc-400 leading-relaxed">
                  <span className="text-amber-400">Unavailable on this device</span> — {deviceReason}. The app still
                  browses, tags, plays and keeps playlists; the {unavailableFeatures(caps).length} features that start
                  another program need a server instead.
                </div>
              )}
              <div className="rounded-md border border-border overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-panel/60">
                    <tr>
                      <th className="th">Tool</th>
                      <th className="th">Status</th>
                      <th className="th">Version</th>
                      <th className="th" title="Newest release upstream has published. An installed tool takes exactly that one when you press Update; only a first install fetches the reviewed pinned version.">
                        Available
                      </th>
                      <th className="th">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(deps?.tools ?? []).map((t) => (
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
                              className={`chip border ${
                                deviceReason || t.installable === false
                                  ? "bg-zinc-800 text-zinc-400 border-zinc-700"
                                  : "bg-red-900/50 text-red-300 border-red-900"
                              }`}
                              title={deviceReason ?? t.install_note ?? undefined}
                            >
                              {deviceReason
                                ? "Unavailable here"
                                : t.installable === false
                                  ? t.install_kind === "system"
                                    ? "System package"
                                    : "No build here"
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
                        <td className="td text-zinc-500" title={t.note ?? ""}>
                          {t.upstream_version ? (
                            <span className={t.update_available ? "text-amber-300" : undefined}>{t.upstream_version}</span>
                          ) : deps?.checking ? (
                            <span className="text-zinc-600 italic">checking…</span>
                          ) : (
                            "—"
                          )}
                        </td>
                        {/* The row's own action, the same one the Dependencies
                            page offers (the backend's `action` field decides):
                            `install`/`update` press this row's install,
                            `upgrade` copies the package manager's command
                            instead — a distro tool this app cannot download
                            over — and `none` is nothing to do here, with the
                            chip and install_note saying why. The row's own
                            note/install_note is the hover text, so the button
                            never promises more than the row it belongs to. */}
                        <td className="td">
                          {t.action === "install" || t.action === "update" ? (
                            <button
                              className="btn-ghost !py-0.5 text-[11px] tap"
                              onClick={() => installDeps([t.key], t.key)}
                              disabled={busy || !!deviceReason}
                              title={
                                deviceReason ??
                                (t.action === "update"
                                  ? t.note ?? (t.upstream_version ? `Upstream: ${t.upstream_version}` : undefined)
                                  : t.install_note ?? t.note ?? undefined)
                              }
                            >
                              {busyDep === t.key ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
                              {t.action === "update" ? "Update" : "Install"}
                            </button>
                          ) : t.upgrade_command ? (
                            <button
                              className="btn-ghost !py-0.5 text-[11px] tap"
                              onClick={() => copyCommand(t.upgrade_command!)}
                              title={t.note ?? t.upgrade_command}
                            >
                              <Copy className="h-3 w-3" /> Copy command
                            </button>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {step.panel === "sources" && (
            <>
              <SourcesPanel />
              {/* The cookie is in the Discovery group below, but HOW to get it
                  is the part nobody remembers a second time. */}
              <details className="rounded-md border border-border bg-bg/40 px-3 py-2">
                <summary className="text-xs font-medium text-zinc-400 cursor-pointer select-none">
                  Getting the RateYourMusic cookie
                </summary>
                <ol className="text-[11px] text-zinc-400 leading-relaxed list-decimal pl-5 mt-1.5 space-y-0.5">
                  <li>Sign in to rateyourmusic.com in your browser (the login is what the scraper borrows).</li>
                  <li>Press <code className="font-mono">F12</code> → <b>Network</b> → reload the page.</li>
                  <li>Click any request to <code className="font-mono">rateyourmusic.com</code> → <b>Headers</b> → <b>Request Headers</b>.</li>
                  <li>Copy everything after <code className="font-mono">Cookie:</code> and paste it into the field below (newlines and a stray <code className="font-mono">Cookie:</code> label are handled).</li>
                  <li>Keep it to yourself — it is your session — and paste a fresh one if RYM later starts refusing.</li>
                </ol>
              </details>
            </>
          )}

          {step.panel === "password" && (
            <form onSubmit={savePassword} className="rounded-md border border-border bg-bg/40 px-3 py-3 space-y-3">
              <div className="text-xs text-zinc-400 leading-relaxed">
                {auth?.has_password
                  ? "This server already asks clients for a password. Change it here; every other session is revoked."
                  : "Nobody has claimed this server yet. Choose the password every client will sign in with — the name is the first user's own."}
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {auth?.has_password && (
                  <label className="block">
                    <span className="text-xs text-zinc-500 uppercase">Current password</span>
                    <input className="input mt-1" type="password" value={currentPw} onChange={(e) => setCurrentPw(e.target.value)} autoComplete="current-password" />
                  </label>
                )}
                <label className="block">
                  <span className="text-xs text-zinc-500 uppercase">New password</span>
                  <input className="input mt-1" type="password" value={newPw} onChange={(e) => setNewPw(e.target.value)} autoComplete="new-password" />
                </label>
                <label className="block">
                  <span className="text-xs text-zinc-500 uppercase">Repeat password</span>
                  <input className="input mt-1" type="password" value={confirmPw} onChange={(e) => setConfirmPw(e.target.value)} autoComplete="new-password" />
                </label>
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-[11px] text-zinc-600">
                  {auth?.has_password ? "The password is never stored in the config — only its hash is." : "Leave this empty and the server stays open on this machine."}
                </span>
                <button className="btn-primary !py-1.5 text-xs" disabled={busy || !newPw || newPw !== confirmPw} title={newPw !== confirmPw ? "The passwords do not match" : undefined}>
                  {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <KeyRound className="h-3.5 w-3.5" />}
                  {auth?.has_password ? "Change password" : "Set password"}
                </button>
              </div>
            </form>
          )}

          {step.panel === "done" && (
            <>
              <div className="rounded-md border border-border bg-bg/60 px-3 py-2 text-xs text-zinc-300 space-y-1">
                <div>
                  Library: <code className="font-mono text-zinc-200">{musicFolder || "(none)"}</code>
                </div>
                <div>
                  {deps
                    ? `${deps.tools.filter((t) => t.state === "ok" || t.state === "update").length}/${deps.tools.length} tools ready`
                    : "Dependency check skipped"}
                  {defaults ? ` · ${offDefaults} settings differ from the shipped defaults` : ""}
                </div>
              </div>
              <p className="text-[11px] text-zinc-500 leading-relaxed">
                Sources and AI keys can be tested and changed anytime in Settings → Sources and Settings → AI; every
                other answer here lives on the matching Settings tab. This wizard stays available from Settings →
                General. Scripts that need missing tools will tell you when you run them.
              </p>
            </>
          )}

          {/* The step's own settings, group by group: the same titles and the
              same fields the Settings page shows, so "where do I change this
              later" has one answer. */}
          {step.groups?.map((title) => {
            const group = cfgGroup(title);
            // A group whose fields are all hidden (Release tracklist: its only
            // setting is a force_* re-run switch) still belongs to a step — the
            // coverage test holds it there — but an empty card would only ask
            // the reader what they are missing.
            if (!group.fields.length) return null;
            const open = !!OPEN_GROUPS[title];
            return (
              <details key={title} className="rounded-md border border-border bg-bg/40 px-3 py-2" open={open}>
                <summary className="text-xs font-semibold text-zinc-300 cursor-pointer select-none">
                  {group.title}
                  {!open && <span className="ml-2 font-normal text-[10px] text-zinc-600">{group.fields.length} settings — the defaults are fine</span>}
                </summary>
                {group.blurb && <p className="text-[11px] text-zinc-500 mt-1 leading-relaxed">{group.blurb}</p>}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2.5 mt-2">{group.fields.map(renderField)}</div>
                {/* The model is the one setting whose value cannot be checked by
                    reading it back, so the AI step keeps its own round trip —
                    sent from the DRAFT, so an unsaved URL/key/model can be
                    tested before either of them is written. */}
                {title === "AI — lyric transforms & genre ranking" && <AiTestButton value={draft} />}
              </details>
            );
          })}

          {/* One footer for every step, and the same two attributes on all of
              them: the wrap keeps a long button label from pushing its partner
              off a phone's card, and `ml-auto` is what right-aligns the right
              group on the line it wraps onto (justify-between leaves a lone
              item at the left edge). */}
          <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
            <button className="btn-ghost" onClick={() => setIndex((i) => Math.max(0, i - 1))} disabled={busy || index === 0}>
              <ArrowLeft className="h-3.5 w-3.5" /> Back
            </button>
            {!isLast && (
              <div className="ml-auto flex flex-wrap justify-end gap-2">
                <button className="btn-ghost" onClick={() => setIndex((i) => i + 1)} disabled={busy}>
                  Skip for now
                </button>
                {step.panel === "folder" || step.panel === "dependencies" ? (
                  <button className="btn-primary" onClick={() => setIndex((i) => i + 1)}>
                    Next <ArrowRight className="h-3.5 w-3.5" />
                  </button>
                ) : (
                  <button className="btn-primary" onClick={() => saveStep(true)} disabled={busy || badCount > 0} title={badCount ? "Fix the highlighted fields first" : undefined}>
                    {busy ? "Saving…" : step.panel === "slskd" ? "Save & start sharing" : "Save & continue"}{" "}
                    <ArrowRight className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
            )}
            {isLast && (
              <button className="btn-primary ml-auto" disabled={busy} onClick={() => exit("/")}>
                {busy ? "Saving…" : "Open library"} <ArrowRight className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
          {badCount > 0 && (
            <div className="text-[11px] text-amber-300">
              {badCount === 1 ? "One value needs a fix" : `${badCount} values need a fix`} before this step can be saved
              — or press Skip for now and keep the defaults.
            </div>
          )}
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
