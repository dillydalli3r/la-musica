import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderOpen, Check, ArrowRight, ArrowLeft, RotateCcw, Users } from "lucide-react";
import { api } from "../api";
import FolderPicker from "../components/FolderPicker";
import { toast } from "../store";

type Step = 1 | 2 | 3 | 4;

export default function SetupPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const [step, setStep] = useState<Step>(1);
  const [musicFolder, setMusicFolder] = useState("");
  const [busy, setBusy] = useState(false);
  // Soulseek sharing setup (step 3)
  const [ssUser, setSsUser] = useState("");
  const [ssPass, setSsPass] = useState("");
  const [ssPort, setSsPort] = useState(50000);
  const [ssShare, setSsShare] = useState(true);
  const [ssAutostart, setSsAutostart] = useState(false);

  const { data: deps, refetch: refetchDeps } = useQuery({
    queryKey: ["dependencies"],
    queryFn: api.dependencies,
    retry: false,
    enabled: step >= 2,
  });

  useEffect(() => {
    if (!config) return;
    if (config.music_folder) setMusicFolder(String(config.music_folder));
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
      setStep(4);
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(false);
    }
  };

  const [showFolderPicker, setShowFolderPicker] = useState(false);
  const pickMusicFolder = async () => {
    try {
      const r = await api.fsPickFolder(musicFolder.trim());
      if (!r.supported) setShowFolderPicker(true); // headless/remote → in-app browser
      else if (r.path) setMusicFolder(r.path);
    } catch {
      setShowFolderPicker(true);
    }
  };

  const installDeps = async (keys?: string[]) => {
    setBusy(true);
    try {
      const r = await api.installDependencies(keys);
      const failed = r.results.filter((x) => !x.ok);
      toast(failed.length ? "Install finished with " + failed.length + " failure(s)" : "Dependencies installed / updated");
      refetchDeps();
    } catch (e) {
      toast(String(e));
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
      toast(String(e));
    } finally {
      setBusy(false);
    }
  };

  const finish = async () => {
    if (!musicFolder.trim()) {
      toast("Pick a music folder first");
      return;
    }
    setBusy(true);
    try {
      await api.saveConfig({ ...config, music_folder: musicFolder.trim(), first_run_done: true });
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["library"] });
      navigate("/", { replace: true });
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen bg-bg text-zinc-100 flex flex-col items-center justify-center p-6">
      <div className="w-full max-w-2xl">
        <div className="flex items-center gap-2 mb-6">
          <img
            src="/icon.png"
            alt="la musica"
            className="h-9 w-9 rounded-md object-cover ring-1 ring-border shadow-sm"
          />
          <div>
            <div className="font-bold tracking-wide">la musica</div>
            <div className="text-xs text-zinc-500">First-run setup</div>
          </div>
        </div>

        <div className="flex items-center gap-2 text-[11px] text-zinc-500 mb-4">
          {([1, 2, 3, 4] as Step[]).map((s) => (
            <div key={s} className="flex items-center gap-2">
              <span
                className={`h-5 w-5 rounded-sm flex items-center justify-center text-[10px] border ${
                  step === s ? "bg-accent text-[var(--accent-fg)] border-accent" : step > s ? "bg-emerald-900/60 text-emerald-300 border-emerald-800" : "bg-panel border-border text-zinc-500"
                }`}
              >
                {step > s ? <Check className="h-3 w-3" /> : s}
              </span>
              <span className={step === s ? "text-zinc-200" : "text-zinc-600"}>
                {s === 1 ? "Music folder" : s === 2 ? "Dependencies" : s === 3 ? "Soulseek" : "Done"}
              </span>
            </div>
          ))}
        </div>

        {step === 1 && (
          <div className="bg-card rounded-lg border border-border p-6 space-y-4">
            <div className="text-sm font-semibold">Where is your music library?</div>
            <p className="text-xs text-zinc-400 leading-relaxed">
              Point this at the folder that contains your artist/album folders (e.g.{" "}
              <code className="font-mono">F:\Music</code>). Everything the app grades, tags and optimizes lives under it.
            </p>
            <label className="block">
              <span className="text-xs text-zinc-500 uppercase">Music folder</span>
              <div className="flex gap-2 mt-1">
                <input
                  className="input"
                  value={musicFolder}
                  onChange={(e) => setMusicFolder(e.target.value)}
                  placeholder="F:\Music"
                />
                <button className="btn-ghost" onClick={pickMusicFolder} title="Browse for a folder">
                  <FolderOpen className="h-4 w-4" />
                </button>
              </div>
            </label>
            {showFolderPicker && (
              <FolderPicker
                initial={musicFolder.trim()}
                onPick={setMusicFolder}
                onClose={() => setShowFolderPicker(false)}
              />
            )}
            <div className="flex items-center justify-between">
              <button className="btn-ghost" onClick={skip} disabled={busy}>
                Skip for now — set it later in Settings
              </button>
              <button className="btn-primary" disabled={!musicFolder.trim()} onClick={() => setStep(2)}>
                Next <ArrowRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        )}

        {step === 2 && (
          <div className="bg-card rounded-lg border border-border p-6 space-y-4">
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
                <button
                  className="btn-ghost !py-1 text-xs"
                  onClick={() => installDeps(deps?.tools.filter((t) => t.state === "missing").map((t) => t.key))}
                  disabled={busy}
                >
                  Install missing
                </button>
                <button className="btn-primary !py-1 text-xs" onClick={() => installDeps()} disabled={busy}>
                  {busy ? "Installing…" : "Install all"}
                </button>
              </div>
            </div>
            <div className="rounded-md border border-border overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-panel/60">
                  <tr>
                    <th className="th">Tool</th>
                    <th className="th">Status</th>
                    <th className="th">Version</th>
                  </tr>
                </thead>
                <tbody>
                  {(deps?.tools ?? []).map((t) => (
                    <tr key={t.key} className="table-row cursor-default">
                      <td className="td font-medium">{t.name}</td>
                      <td className="td">
                        {t.state === "ok" && <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800">ready</span>}
                        {t.state === "update" && <span className="chip bg-amber-900/50 text-amber-300 border border-amber-900">update</span>}
                        {t.state === "missing" && <span className="chip bg-red-900/50 text-red-300 border border-red-900">missing</span>}
                      </td>
                      <td className="td text-zinc-500">{t.installed_version ?? t.detected_version ?? t.latest_version ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex justify-between">
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
          <div className="bg-card rounded-lg border border-border p-6 space-y-4">
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
            <div className="flex items-center justify-between">
              <button className="btn-ghost" onClick={() => setStep(2)} disabled={busy}>
                <ArrowLeft className="h-3.5 w-3.5" /> Back
              </button>
              <div className="flex gap-2">
                <button className="btn-ghost" onClick={() => setStep(4)} disabled={busy}>
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

        {step === 4 && (
          <div className="bg-card rounded-lg border border-border p-6 space-y-4">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <Check className="h-4 w-4 text-emerald-400" /> You're all set
            </div>
            <p className="text-xs text-zinc-400 leading-relaxed">
              Library: <code className="font-mono text-zinc-200">{musicFolder || "(none)"}</code>
              <br />
              {deps ? `${deps.tools.filter((t) => t.state === "ok" || t.state === "update").length}/${deps.tools.length} tools ready` : "Dependency check skipped"}.
              Scripts that need missing tools will tell you when you run them.
            </p>
            <div className="flex justify-end">
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