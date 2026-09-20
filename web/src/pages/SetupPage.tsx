import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ArrowRight, ArrowLeft, RotateCcw, Users, Sparkles, Music2 } from "lucide-react";
import { api, deviceUnavailable, installSummary, unavailableFeatures } from "../api";
import SourcesPanel from "../components/SourcesPanel";
import AiTestButton from "../components/AiTestButton";
import { toast } from "../store";

type Step = 1 | 2 | 3 | 4 | 5 | 6;

export default function SetupPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const [step, setStep] = useState<Step>(1);
  const [musicFolder, setMusicFolder] = useState("");
  const [busy, setBusy] = useState(false);
  // AI lyric transforms (step 4) — the keys script 17 reads.
  const [ai, setAi] = useState<Record<string, unknown>>({});
  const [rymLinksAuto, setRymLinksAuto] = useState(true);
  // Soulseek sharing setup (step 5)
  const [ssUser, setSsUser] = useState("");
  const [ssPass, setSsPass] = useState("");
  const [ssPort, setSsPort] = useState(50000);
  const [ssShare, setSsShare] = useState(true);
  const [ssAutostart, setSsAutostart] = useState(false);

  const { data: deps, refetch: refetchDeps } = useQuery({
    queryKey: ["dependencies"],
    queryFn: () => api.dependencies(),
    retry: false,
    enabled: step >= 2,
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
    enabled: step >= 2,
    staleTime: 5 * 60 * 1000,
  });
  const deviceReason = deviceUnavailable(caps);
  // What the "Install N missing" button would install, so the wizard only
  // offers it when there is something to install — and only what this host can
  // actually fetch: a Windows-only tool on Linux, or one the image already
  // provides as a distro package, has nothing to download (the row's
  // `installable`), so counting it would offer a button that can only fail.
  const missing = (deps?.tools ?? []).filter((t) => t.state === "missing" && t.installable !== false);

  useEffect(() => {
    if (!config) return;
    if (config.music_folder) setMusicFolder(String(config.music_folder));
    setAi({
      ai_base_url: String(config.ai_base_url ?? ""),
      ai_api_key: String(config.ai_api_key ?? ""),
      ai_model: String(config.ai_model ?? ""),
      ai_effort: String(config.ai_effort ?? "high"),
      lyrics_translation_langs: String(config.lyrics_translation_langs ?? "en"),
      lyrics_xlit_enabled: config.lyrics_xlit_enabled !== false,
      lyrics_translate_enabled: config.lyrics_translate_enabled !== false,
      lyrics_xlit_sidecars: config.lyrics_xlit_sidecars !== false,
    });
    setRymLinksAuto(config.rym_links_auto !== false);
    setSsUser(String(config.soulseek_username ?? ""));
    setSsPass(String(config.soulseek_password ?? ""));
    setSsPort(Number(config.soulseek_listen_port ?? 50000));
    setSsShare(config.soulseek_share_library !== false);
    setSsAutostart(!!config.soulseek_autostart);
  }, [config]);

  const saveSoulseek = async (andStart: boolean) => {
    setBusy(true);
    try {
      await api.saveConfig({
        ...config,
        soulseek_username: ssUser.trim(),
        soulseek_password: ssPass,
        soulseek_listen_port: ssPort,
        soulseek_share_library: ssShare,
        soulseek_autostart: ssAutostart,
      });
      qc.invalidateQueries({ queryKey: ["config"] });
      if (andStart) {
        if (ssShare) await api.soulseekSharesRefresh().catch(() => undefined);
        await api.soulseekStart();
        toast("slskd started — sharing your library");
      } else {
        toast("Soulseek settings saved");
      }
      setStep(6);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const saveAi = async (advance: boolean) => {
    setBusy(true);
    try {
      await api.saveConfig({ ...config, ...ai, rym_links_auto: rymLinksAuto });
      qc.invalidateQueries({ queryKey: ["config"] });
      toast.success("Saved");
      if (advance) setStep(5);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const installDeps = async (keys?: string[]) => {
    setBusy(true);
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
    }
  };

  const skip = async () => {
    setBusy(true);
    try {
      await api.saveConfig({ ...config, first_run_done: true });
      qc.invalidateQueries({ queryKey: ["config"] });
      navigate("/", { replace: true });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const finish = async () => {
    setBusy(true);
    try {
      // The music folder is startup-owned (MLO_MUSIC_FOLDER / config.json),
      // never written from the UI — this step only marks the wizard done.
      await api.saveConfig({ ...config, first_run_done: true });
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["library"] });
      navigate("/", { replace: true });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-dvh bg-bg text-zinc-100 flex flex-col items-center justify-center p-6">
      <div className="w-full max-w-2xl">
        <div className="flex items-center gap-2 mb-6">
          <img
            src="/icon.png"
            alt="la musica"
            className="h-9 w-9 rounded-md object-cover ring-1 ring-border shadow-sm"
          />
          <div>
            <div className="font-bold tracking-wide">la musica</div>
            <div className="text-xs text-zinc-500">{config?.first_run_done ? "Setup" : "First-run setup"}</div>
          </div>
        </div>

        {/* Same rail as the client wizard's, and wrapped for the same reason:
            six labelled steps are 474px of content in a 390px column, so
            Soulseek and Done used to sit past the right edge, reachable only by
            sideways-scrolling the page's own scroll container. */}
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-zinc-500 mb-4">
          {([1, 2, 3, 4, 5, 6] as Step[]).map((s) => (
            <div key={s} className="flex items-center gap-2">
              <span
                className={`h-5 w-5 rounded-sm flex items-center justify-center text-[10px] border ${
                  step === s ? "bg-accent text-[var(--accent-fg)] border-accent" : step > s ? "bg-emerald-900/60 text-emerald-300 border-emerald-800" : "bg-panel border-border text-zinc-500"
                }`}
              >
                {step > s ? <Check className="h-3 w-3" /> : s}
              </span>
              <span className={step === s ? "text-zinc-200" : "text-zinc-600"}>
                {s === 1 ? "Music folder" : s === 2 ? "Dependencies" : s === 3 ? "Sources" : s === 4 ? "AI & RYM" : s === 5 ? "Soulseek" : "Done"}
              </span>
            </div>
          ))}
        </div>

        {step === 1 && (
          <div className="panel p-6 space-y-4">
            <div className="text-sm font-semibold">Your music library</div>
            <p className="text-xs text-zinc-400 leading-relaxed">
              Everything the app grades, tags and optimizes lives under one folder (your artist/album tree). It is
              chosen when the app starts — this step only shows what it resolved to.
            </p>
            <div className="rounded-md border border-border bg-bg/60 px-3 py-2">
              <div className="text-xs text-zinc-500 uppercase">Music folder</div>
              <div className="font-mono text-xs text-zinc-200 break-all mt-1">
                {musicFolder.trim() || "not configured yet"}
              </div>
              <div className="text-[11px] text-zinc-500 mt-1 leading-relaxed">
                Fixed at startup, not editable here: set <code className="font-mono">MLO_MUSIC_FOLDER</code> (Docker /
                compose) or <code className="font-mono">music_folder</code> in config.json, then restart. Settings →
                General shows the resolved folder.
              </div>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <button className="btn-ghost" onClick={skip} disabled={busy}>
                Skip for now
              </button>
              <button className="btn-primary ml-auto" disabled={busy} onClick={() => setStep(2)}>
                Next <ArrowRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        )}

        {step === 2 && (
          <div className="panel p-6 space-y-4">
            <div className="flex items-center justify-between flex-wrap gap-2">
              <div>
                <div className="text-sm font-semibold">External tools</div>
                <p className="text-xs text-zinc-400 mt-1">
                  The scripts need these tools. Missing ones are downloaded into the app's dependencies folder.
                </p>
              </div>
              <div className="flex gap-2">
                <button className="btn-ghost !py-1 text-xs" onClick={() => refetchDeps()} disabled={busy}>
                  <RotateCcw className="h-3 w-3" /> Refresh
                </button>
                {/* Only offered when there IS something missing: with an empty
                    list the press sent `keys: []`, which the server read as
                    "every tool" and answered with a full reinstall of all
                    sixteen. */}
                {missing.length > 0 && (
                  <button
                    className="btn-ghost !py-1 text-xs"
                    onClick={() => installDeps(missing.map((t) => t.key))}
                    disabled={busy || !!deviceReason}
                    title={deviceReason ?? undefined}
                  >
                    Install {missing.length} missing
                  </button>
                )}
                <button
                  className="btn-primary !py-1 text-xs"
                  onClick={() => installDeps()}
                  disabled={busy || !!deviceReason}
                  title={deviceReason ?? undefined}
                >
                  {busy ? "Installing…" : "Install all"}
                </button>
              </div>
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
                    <th className="th" title="Newest release upstream has published. Install still fetches the reviewed pinned version.">
                      Available
                    </th>
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
                      <td className="td text-zinc-500" title={t.note ?? ""}>
                        {t.upstream_version ?? (deps?.checking ? "checking…" : "—")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <button className="btn-ghost" onClick={() => setStep(1)}>
                <ArrowLeft className="h-3.5 w-3.5" /> Back
              </button>
              <button className="btn-primary" onClick={() => setStep(3)}>
                Next <ArrowRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        )}

        {step === 3 && (
          <div className="panel p-6 space-y-4">
            <div>
              <div className="text-sm font-semibold">Sources</div>
              <p className="text-xs text-zinc-400 mt-1">
                What the app asks for lyrics, genres, ratings and artwork. All of them are free; the
                keyed ones work without keys too — they just get skipped. Nothing here blocks the
                rest of the setup.
              </p>
            </div>
            <SourcesPanel />
            <div className="flex flex-wrap items-center justify-between gap-2">
              <button className="btn-ghost" onClick={() => setStep(2)}>
                <ArrowLeft className="h-3.5 w-3.5" /> Back
              </button>
              <div className="ml-auto flex flex-wrap justify-end gap-2">
                <button className="btn-ghost" onClick={() => setStep(4)}>
                  Skip for now
                </button>
                <button className="btn-primary" onClick={() => setStep(4)}>
                  Next <ArrowRight className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>
        )}

        {step === 4 && (
          <div className="panel p-6 space-y-4">
            <div>
              <div className="flex items-center gap-2 text-sm font-semibold">
                <Sparkles className="h-4 w-4 text-accent" /> AI lyric transliteration &amp; translation
              </div>
              <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
                Optional, and the only model in the app. Script 17 romanizes non-Latin lyrics and
                translates them into your languages, storing the results as
                <code className="font-mono"> TRANSLITERATION-*</code> /
                <code className="font-mono"> TRANSLATION-*</code> tags (the player shows them as
                sub-lines under each lyric line). Any OpenAI-compatible endpoint works — OpenAI,
                OpenRouter, LM Studio, llama.cpp, or Google Gemini's OpenAI-compatible endpoint.
                Leave the URL or model empty and the script simply skips.
              </p>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">Base URL</span>
                <input
                  className="input mt-1"
                  value={String(ai.ai_base_url ?? "")}
                  onChange={(e) => setAi({ ...ai, ai_base_url: e.target.value })}
                  placeholder="https://api.openai.com/v1"
                />
              </label>
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">API key</span>
                <input
                  className="input mt-1"
                  type="password"
                  value={String(ai.ai_api_key ?? "")}
                  onChange={(e) => setAi({ ...ai, ai_api_key: e.target.value })}
                  placeholder="leave empty for a local server"
                />
              </label>
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">Model</span>
                <input
                  className="input mt-1"
                  value={String(ai.ai_model ?? "")}
                  onChange={(e) => setAi({ ...ai, ai_model: e.target.value })}
                  placeholder="gpt-4o-mini · gemini-2.5-flash · local-model"
                />
              </label>
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">Translate into</span>
                <input
                  className="input mt-1"
                  value={String(ai.lyrics_translation_langs ?? "en")}
                  onChange={(e) => setAi({ ...ai, lyrics_translation_langs: e.target.value })}
                  placeholder="en,de"
                />
                <span className="text-[10px] text-zinc-600">
                  The first language is yours — it decides when romanizing is worth doing.
                </span>
              </label>
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">Reasoning effort</span>
                <select
                  className="input mt-1"
                  value={String(ai.ai_effort ?? "high")}
                  onChange={(e) => setAi({ ...ai, ai_effort: e.target.value })}
                >
                  <option value="high">High — best quality</option>
                  <option value="medium">Medium</option>
                  <option value="low">Low</option>
                  <option value="minimal">Minimal — no thinking, fastest</option>
                </select>
              </label>
              <div className="flex flex-col justify-end gap-1.5 pb-1">
                <label className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer">
                  <input type="checkbox" className="accent-[var(--accent)]"
                    checked={ai.lyrics_xlit_enabled !== false}
                    onChange={(e) => setAi({ ...ai, lyrics_xlit_enabled: e.target.checked })} />
                  Transliterate non-Latin lyrics
                </label>
                <label className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer">
                  <input type="checkbox" className="accent-[var(--accent)]"
                    checked={ai.lyrics_translate_enabled !== false}
                    onChange={(e) => setAi({ ...ai, lyrics_translate_enabled: e.target.checked })} />
                  Translate lyrics
                </label>
                <label className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer">
                  <input type="checkbox" className="accent-[var(--accent)]"
                    checked={ai.lyrics_xlit_sidecars !== false}
                    onChange={(e) => setAi({ ...ai, lyrics_xlit_sidecars: e.target.checked })} />
                  Write .romaji.lrc / .&lt;lang&gt;.lrc sidecars
                </label>
              </div>
            </div>
            <AiTestButton value={ai} />

            <div className="pt-3 border-t border-border space-y-2">
              <div className="flex items-center gap-2 text-sm font-semibold">
                <Music2 className="h-4 w-4 text-accent" /> RateYourMusic links
              </div>
              <p className="text-xs text-zinc-400 leading-relaxed">
                RYM states the album and artist page for a release, and every import stamps that pair
                onto its tracks. MusicBrainz answers for well-known releases with no setup; for the rest
                RYM has to be scraped, and it only answers a signed-in browser session.
              </p>
              <ol className="text-xs text-zinc-400 leading-relaxed list-decimal pl-5 space-y-0.5">
                <li>Sign in to rateyourmusic.com in your browser (the login is what the scraper borrows).</li>
                <li>Press <code className="font-mono">F12</code> → <b>Network</b> → reload the page.</li>
                <li>
                  Click any request to <code className="font-mono">rateyourmusic.com</code> → <b>Headers</b> →{" "}
                  <b>Request Headers</b>.
                </li>
                <li>
                  Copy everything after <code className="font-mono">Cookie:</code> and paste it in the field
                  under <b>RateYourMusic links</b> below (newlines and a stray{" "}
                  <code className="font-mono">Cookie:</code> label are handled — the whole value is fine).
                </li>
                <li>
                  Press <b>Save &amp; test</b>: it resolves a real album+artist pair, so "did my cookie work?"
                  has a yes/no answer. Keep the value to yourself — it is your session — and paste a fresh one
                  if RYM later starts refusing (signing out invalidates it).
                </li>
              </ol>
              <SourcesPanel only="links" />
              <label className="flex items-start gap-2 text-xs text-zinc-300 cursor-pointer pt-1">
                <input type="checkbox" className="mt-0.5 accent-[var(--accent)]"
                  checked={rymLinksAuto}
                  onChange={(e) => setRymLinksAuto(e.target.checked)} />
                <span>
                  Look the links up automatically during imports
                  <span className="block text-[10px] text-zinc-600">
                    An existing link is never overwritten, and a blocked RYM leaves the import untouched.
                  </span>
                </span>
              </label>
            </div>

            {/* Wrap plus a right-group ml-auto is the whole fit fix for this
                file's widest footers: "Skip for now" beside "Save & continue"
                used to squeeze both buttons until their labels broke into two
                lines. Every footer below carries it. */}
            <div className="flex flex-wrap items-center justify-between gap-2">
              <button className="btn-ghost" onClick={() => setStep(3)} disabled={busy}>
                <ArrowLeft className="h-3.5 w-3.5" /> Back
              </button>
              <div className="ml-auto flex flex-wrap justify-end gap-2">
                <button className="btn-ghost" onClick={() => setStep(5)} disabled={busy}>
                  Skip for now
                </button>
                <button className="btn-primary" onClick={() => saveAi(true)} disabled={busy}>
                  {busy ? "Saving…" : "Save & continue"} <ArrowRight className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>
        )}

        {step === 5 && (
          <div className="panel p-6 space-y-4">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <Users className="h-4 w-4 text-accent" /> Share your library on Soulseek
            </div>
            <p className="text-xs text-zinc-400 leading-relaxed">
              Your music folder is shared with the network on the listen port below — slskd is
              restarted automatically whenever files are added, removed or reorganized so the
              share index always matches the disk. You can turn sharing off anytime in
              Settings → Soulseek.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">Soulseek username</span>
                <input className="input mt-1" value={ssUser} onChange={(e) => setSsUser(e.target.value)} placeholder="your-nick" />
              </label>
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">Password</span>
                <input className="input mt-1" type="password" value={ssPass} onChange={(e) => setSsPass(e.target.value)} placeholder="••••••••" />
              </label>
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">Listen port</span>
                <input className="input mt-1" type="number" value={ssPort} min={1024} max={65535}
                  onChange={(e) => setSsPort(Number(e.target.value))} />
              </label>
              <div className="flex flex-col justify-end gap-1.5 pb-1">
                <label className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer">
                  <input type="checkbox" className="accent-[var(--accent)]" checked={ssShare} onChange={() => setSsShare(!ssShare)} />
                  Share the library
                </label>
                <label className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer">
                  <input type="checkbox" className="accent-[var(--accent)]" checked={ssAutostart} onChange={() => setSsAutostart(!ssAutostart)} />
                  Start slskd with the app
                </label>
              </div>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <button className="btn-ghost" onClick={() => setStep(4)} disabled={busy}>
                <ArrowLeft className="h-3.5 w-3.5" /> Back
              </button>
              <div className="ml-auto flex flex-wrap justify-end gap-2">
                <button className="btn-ghost" onClick={() => setStep(6)} disabled={busy}>
                  Skip for now
                </button>
                <button className="btn-primary" disabled={busy || !ssShare} onClick={() => saveSoulseek(true)}
                  title={ssShare ? "Save settings and start sharing" : "Sharing is off — use Skip"}>
                  {busy ? "Saving…" : ssShare ? "Save & start sharing" : "Save"} <ArrowRight className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>
        )}

        {step === 6 && (
          <div className="panel p-6 space-y-4">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <Check className="h-4 w-4 text-emerald-400" /> You're all set
            </div>
            <p className="text-xs text-zinc-400 leading-relaxed">
              Library: <code className="font-mono text-zinc-200">{musicFolder || "(none)"}</code>
              <br />
              {deps ? `${deps.tools.filter((t) => t.state === "ok" || t.state === "update").length}/${deps.tools.length} tools ready` : "Dependency check skipped"}.
              Sources and AI keys can be tested and changed anytime in Settings → Sources and Settings → AI.
              This wizard stays available from Settings → General. Scripts that need missing tools will tell you when you run them.
            </p>
            <div className="flex flex-wrap justify-end">
              <button className="btn-primary" disabled={busy} onClick={finish}>
                {busy ? "Saving…" : "Open library"} <ArrowRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}