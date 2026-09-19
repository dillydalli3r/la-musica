import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ClipboardCheck, RefreshCw, RotateCcw, Save, ShieldCheck, SunMedium, ToggleRight } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import ConfirmButton from "../components/ConfirmButton";
import Segmented from "../components/Segmented";
import { PageLoading } from "../components/Badges";
import PageHeader from "../components/PageHeader";

/** In-depth grading configuration: every check that can count for or
 * against grading, grouped the way they apply — track/album checks,
 * auditing, links, covers, strict formatting, lyrics & translations and
 * the file categories that participate in album grading at all. Toggles
 * edit a local copy; Save writes the whole grading block via saveConfig. */

interface CheckDef {
  k: string;
  label: string;
  desc: string;
}

interface Group {
  id: string;
  title: string;
  desc: string;
  items: CheckDef[];
}

const GROUPS: Group[] = [
  {
    id: "tracks",
    title: "Tracks & albums",
    desc: "Core checks applied to every track and album in the library.",
    items: [
      { k: "grade_check_unreadable", label: "Unreadable files", desc: "Files that can't be opened or decoded fail the album." },
      { k: "grade_check_missing_tags", label: "Required tags", desc: "Every required per-track tag (title, artist, date, …) must exist and be non-empty." },
      { k: "grade_check_album_tags", label: "Album-level tags", desc: "Album-wide tags (album, album artist, catalog number, …) must be present on the tracks." },
      { k: "grade_check_mood", label: "Mood tag present", desc: "Every track needs a MOOD tag — script 8 fills it (script 16 re-runs just that classifier), so no track should ship without one (issue code MOOD_MISSING)." },
      { k: "grade_check_energy", label: "Energy tag present", desc: "Every track needs an ENERGY tag (0-100, written with MOOD by script 8 or 16; issue code ENERGY_MISSING)." },
      { k: "grade_check_genre", label: "Genre tag present", desc: "Every track needs a GENRE tag. Graded on its own, independent of the required-tags sweep (issue code GENRE_MISSING)." },
      { k: "grade_check_genre_count", label: "Genre count per track", desc: "A track may hold AT MOST the number of genres set by 'Genres per track' in Settings → Import & tags (mb_genre_count) — only an overflow fails (issue code GENRE_COUNT). Fewer is fine: the family is derived from the specific genre, so one specific genre is a complete answer and nothing is topped up with filler." },
      { k: "grade_check_genre_order", label: "Genre order (family last)", desc: "The family, if present, must be the LAST genre — shoegaze / dream pop / rock. A family in an earlier slot, or a genre repeated, fails (issue code GENRE_ORDER). The names themselves are graded by the vocabulary check below." },
      { k: "grade_check_genre_vocab", label: "Genre vocabulary", desc: "Every GENRE name must be one MusicBrainz publishes (shoegaze, dream pop, …). A name it does not know fails with issue code GENRE_VOCAB and is named in the report — the writers keep what a source said, so grading is where it surfaces. Grading never rewrites the tag: run Auto tagging (8) or Format all (10) to canonicalize it." },
      { k: "grade_check_replaygain", label: "ReplayGain tags present", desc: "A file that carries any REPLAYGAIN_* tag must carry all four — REPLAYGAIN_TRACK_GAIN/_PEAK and _ALBUM_GAIN/_PEAK. A file with none is not graded (run the Loudness pass; the player can also analyse on demand)." },
      { k: "grade_check_encoder", label: "Encoder identity", desc: "The ENCODER_* markers switched on under Tagging → Encoder tags must be present (PROGRAM is off by default). Covers are graded by the same rule while image processing is on." },
      { k: "grade_check_naming", label: "Naming script match", desc: "File paths must match the configured naming script (full or shortened MusicBrainz IDs both accepted)." },
      { k: "grade_check_filename_case", label: "Path capitalization", desc: "Filenames and folder names must match the naming script's letter case exactly — TOXICITY vs Toxicity fails. Organize applies the canonical casing." },
      { k: "grade_check_ext_case", label: "Lowercase extensions", desc: "File extensions must be lowercase (01 - Song.FLAC fails). Organize lowercases every extension it touches." },
      { k: "grade_check_key_bpm", label: "Key & BPM", desc: "INITIALKEY and BPM tags (written by script 12) are required." },
      { k: "grade_check_acoustid", label: "AcoustID tags present", desc: "Files already carrying ACOUSTID_ID or ACOUSTID_FINGERPRINT must keep both — a library without them is never graded." },
      { k: "grade_check_excess_tags", label: "Excess tags", desc: "Any tag the optimizer would strip — outside the known tag set — fails the track. Run Optimization to remove them." },
      { k: "grade_check_media", label: "Media type", desc: "The MEDIA tag must be present and consistent with the release." },
      { k: "grade_check_source", label: "Source tag", desc: "The SOURCE tag must be present (with different rules for CD vs digital releases)." },
      { k: "grade_check_instrumental", label: "Instrumental consistency", desc: "INSTRUMENTAL=1 tracks must not carry lyrics; INSTRUMENTAL=0 tracks are graded for lyrics below." },
      { k: "grade_check_disallowed", label: "Disallowed file types", desc: "Unclassified files (.txt, .pdf, .m3u, …) fail the album unless their category is enabled under File categories." },
      { k: "grade_check_extra_images", label: "Stray images", desc: "Images that are neither cover.* nor per-track sidecars fail the album." },
      { k: "grade_check_empty_folders", label: "Empty folders", desc: "A folder with no audio track anywhere beneath it fails the run (issue code EMPTY_FOLDER) — albums come from audio files, so such a folder would otherwise be skipped silently. A folder still holding part of the album (cover.*, .cue, .log, .lrc, .accurip) counts too: its audio is gone. Hidden and app-state folders are ignored." },
      { k: "grade_check_expected_tracks", label: "Release tracklist manifest", desc: "An album that carries a MusicBrainz release id but no .mlo_expected.json fails the run (issue code EXPECTED_TRACKS_MISSING). The manifest records the release's own tracklist, which is the only way a partial import can name the tracks that never arrived; script 15 (Release tracklist) writes it, and the album page greys out the missing tracks from it. An album with no release id is not graded on it — script 15 writes no manifest without one, so the check could never be cleared." },
      { k: "grade_check_album_description", label: "Album description stored", desc: "The album folder needs a non-blank description.txt — fetch one on the album page." },
      { k: "grade_check_raw_video", label: "Raw videos", desc: "Un-remuxed videos (VOB/AVI/WMV/TS) fail — run script 11 to normalize them to MKV." },
      { k: "grade_check_lossless_source", label: "Lossless sources", desc: "Uncompressed lossless sources (WAV/AIFF/APE/WV/SHN) fail — script 3 converts them to FLAC." },
      { k: "grade_check_disc_naming", label: "Disc rip-sheet naming", desc: "A CD's .log / .cue / .accurip files must follow the configured disc pattern (CD-1, CD-2 … by default). The check runs whatever auto-rename is set to — with it off, renaming them is a manual job (the issue text says so). Disc FOLDERS are the layout scan's business: it reports a folder inside an album that is not a disc folder, and the naming script covers their letter case." },
      { k: "grade_check_cd_log", label: "CD — .log present", desc: "Every CD disc needs an exact-match .log file." },
      { k: "grade_check_cd_cue", label: "CD — .cue present", desc: "Every CD disc needs a .cue sheet." },
      { k: "grade_check_cd_format", label: "CD — lossless format", desc: "CD tracks must be FLAC (lossless)." },
      { k: "grade_check_crc", label: "CRC checksums", desc: "Every track must be covered by a per-track CRC in its own disc's .log, and that CRC must match the CRC of the track's decoded audio — coverage alone is not enough (issue codes CRC / CRC_MISMATCH)." },
    ],
  },
  {
    id: "artist",
    title: "Artist",
    desc: "Graded once per ARTIST folder, not per album — the artist page shows the same two checks and its own badge.",
    items: [
      { k: "grade_check_artist_image", label: "Artist image stored", desc: "The artist folder must hold an artist.jpg / artist.png (issue code ARTIST_IMAGE_MISSING)." },
      { k: "grade_check_artist_description", label: "Artist description stored", desc: "The artist folder must hold a non-blank description.txt (issue code ARTIST_DESCRIPTION_MISSING)." },
    ],
  },
  {
    id: "auditing",
    title: "Auditing",
    desc: "Audio verification (script 6) and the log scores it produces. With 'Require audit tag' on, AUDIT must read REAL — a FAKE or MIX verdict fails the track; off, the verdict does not change the grade. AccurateRip is AUDIT-ONLY: a .accurip that mismatches the reference database (or is missing) never fails grading on its own — it turns the AUDIT column FAKE, which is what fails the album once 'Require audit tag' is on.",
    items: [
      { k: "grade_check_audit", label: "Require audit tag", desc: "Tracks must carry an AUDIT tag (run Audit Library). Off by default so unaudited libraries aren't auto-failed." },
      { k: "grade_check_log_checksum", label: "Log checksum valid", desc: "The rip .log's own EAC SHA256 must verify. A log that does not verify — or that states no checksum while 'verify log checksum' is on — fails grading (issue code LOG_CHECKSUM), independently of the audit tag. XLD and older EAC logs that carry no checksum concept pass." },
      { k: "grade_check_accuraterip", label: "AccurateRip verified (audit only)", desc: "A .accurip whose verdict is not REAL marks the album's AUDIT FAKE (and each affected track red) — grading itself is reserved to tagging, so this key never costs a grade point. Turn on 'Require audit tag' for that verdict to fail the album." },
      { k: "grade_check_log_grade", label: "Log grade present & in range", desc: "LOG_GRADE tag must exist and be 0–100." },
    ],
  },
  {
    id: "links",
    title: "Identity links",
    desc: "The two release-level identity links, graded per track.",
    items: [
      { k: "grade_check_mb_links", label: "MusicBrainz release link", desc: "The MusicBrainz release (or its release group) must be tagged." },
      { k: "grade_check_rym_links", label: "RateYourMusic release link", desc: "The RateYourMusic release page URL must be tagged." },
    ],
  },
  {
    id: "covers",
    title: "Covers",
    desc: "Cover art presence and the size / square rules from Settings → Images.",
    items: [
      { k: "grade_check_cover", label: "Cover art", desc: "The album must have cover art meeting the configured size, squareness and crop rules." },
      { k: "grade_check_cover_crop", label: "Cover aspect ratio (squareness)", desc: "The cover's width/height must be square within cover_crop_threshold — an aspect-ratio test, not crop detection (issue: 'Cover aspect ratio WxH not square')." },
      { k: "grade_check_sidecar_cover", label: "Per-track sidecar covers", desc: "Sidecar covers (01 - Song.jpg) must meet the same cover rules." },
    ],
  },
  {
    id: "formatting",
    title: "Strict formatting",
    desc: "Whitespace / blank-line / canonical-form rules. These make near-miss files fail so the formatter scripts can fix them.",
    items: [
      { k: "grade_check_tag_spaces", label: "Tags — no padding", desc: "Leading/trailing spaces or tabs in any tag value fail." },
      { k: "grade_check_tag_blank_lines", label: "Tags — no blank lines", desc: "Blank lines inside tag values fail (LYRICS is exempt — its own rules apply)." },
      { k: "grade_check_lyrics_spaces", label: "Lyrics — no padding", desc: "Leading/trailing spaces on lyric lines fail." },
      { k: "grade_check_lyrics_blank_lines", label: "Lyrics — blank line rules", desc: "Blank-line placement must match the lyrics formatter's canonical output." },
      { k: "grade_check_lyrics_zero", label: "Lyrics — zero timestamp rule", desc: "The [00:00.00] leader line must follow the configured lyrics rules." },
      { k: "grade_check_lyrics_format", label: "Lyrics — canonical formatting", desc: "Stored lyrics must exactly match what the Format Lyrics script would produce." },
      { k: "grade_check_cue_spaces", label: "CUE — no padding", desc: "Leading/trailing spaces in CUE lines fail." },
      { k: "grade_check_cue_blank_lines", label: "CUE — no blank lines", desc: "Blank lines in CUE sheets fail." },
      { k: "grade_check_cue_format", label: "CUE — canonical formatting", desc: "CUE sheets must match the canonical formatter output." },
      { k: "grade_check_accurip_format", label: ".accurip — canonical formatting", desc: ".accurip files must match the canonical shape (each line trimmed, outer blank lines handled) that the AccurateRip / Format All scripts write — run one of them to fix it." },
      { k: "grade_check_cue_files", label: "CUE — referenced files exist", desc: "Every file a CUE's FILE line names must exist in the album. The formatter carries names through verbatim, so a converted (wav→flac) or renamed album keeps a sheet pointing at a file that is not there (run the CUE Sheets script)." },
    ],
  },
  {
    id: "lyrics",
    title: "Lyrics & translations",
    desc: "Presence checks for lyrics and the translation/transliteration tags stored beside them (TRANSLATION-EN, TRANSLITERATION-JA-LATN, sidecars).",
    items: [
      { k: "grade_check_lyrics", label: "Lyrics present", desc: "Every non-instrumental track needs lyrics (embedded and/or .lrc sidecar, per the lyrics format)." },
      { k: "grade_check_lyrics_lang_tags", label: "Transform language tags", desc: "Transform tags must carry their language (TRANSLATION-EN, TRANSLITERATION-JA-LATN — never the bare legacy names)." },
      { k: "grade_check_xlit_transliteration", label: "Transliteration — needed, never extra", desc: "A track whose lyrics are already Latin script must NOT carry a TRANSLITERATION tag (or a .romaji.lrc sidecar) — that fails as XLIT_UNNEEDED — while non-Latin lyrics must have one, or it fails as XLIT_MISSING. Instrumental tracks are never graded on it." },
      { k: "grade_check_xlit_translation", label: "Translation — needed, never extra", desc: "Computed against the reader's language (the first of 'Lyrics translation languages'): a track already in that language must not carry a TRANSLATION tag or a .<lang>.lrc sidecar (XLIT_UNNEEDED), and one that is not must have the right one (XLIT_MISSING, which also names a mismatch like TRANSLATION-DE stored for English lyrics)." },
    ],
  },
  {
    id: "categories",
    title: "File categories",
    desc: "Which file types participate in album grading. Turning a category off stops its own checks AND marks those files as disallowed — the 'Disallowed file types' toggle above decides whether that fails the album.",
    items: [
      { k: "grade_include_music", label: "Audio tracks", desc: "The music files themselves." },
      { k: "grade_include_cover", label: "Cover art", desc: "cover.* images count toward the grade." },
      { k: "grade_include_description", label: "Album description", desc: "description.txt (fetched on the album page) counts as the app's own file rather than a stray one." },
      { k: "grade_include_cue", label: "CUE sheets", desc: ".cue sidecars count toward the grade." },
      { k: "grade_include_log", label: "Log files", desc: ".log sidecars count toward the grade." },
      { k: "grade_include_lrc", label: "LRC lyrics", desc: ".lrc sidecars count toward the grade." },
      { k: "grade_include_accurip", label: "AccurateRip files", desc: ".accurip files count toward the grade." },
      { k: "grade_include_video", label: "Remuxed videos", desc: "MKV/MP4 music videos count toward the grade." },
      { k: "grade_include_other", label: "Other files", desc: "Anything unclassified counts toward the grade. Off by default." },
    ],
  },
];

/** Numeric settings shown alongside the toggles. */
const NUMBERS: { k: string; label: string; desc: string; min: number; max: number }[] = [
  {
    k: "grade_log_score_threshold",
    label: "Logchecker score threshold",
    desc: "LOG_GRADE must be at least this value when the master log-grade check is on. 0 disables the threshold.",
    min: 0,
    max: 100,
  },
];

/** Every grading key this page owns — the reset-to-defaults scope. */
const GRADING_KEYS = [
  ...GROUPS.flatMap((g) => g.items.map((i) => i.k)),
  ...NUMBERS.map((n) => n.k),
];

/** The same list WITHOUT the numeric settings: Enable/Disable all may only
 *  write booleans. Writing them over grade_log_score_threshold turned a tuned
 *  threshold into `false` (rendered as 0, which disables the check). */
const GRADING_TOGGLES = GROUPS.flatMap((g) => g.items.map((i) => i.k));

/** The real toggles, minus the file-category permissions: a preset may force
 * checks on, but choosing what counts toward a grade is the user's call. */
const CHECK_KEYS = GROUPS.filter((g) => g.id !== "categories").flatMap((g) => g.items.map((i) => i.k));

export default function GradingPage() {
  const { data: config, isError: configError, refetch: refetchConfig } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
  });
  const { data: defaults } = useQuery({ queryKey: ["configDefaults"], queryFn: api.configDefaults });
  const qc = useQueryClient();
  const [local, setLocal] = useState<Record<string, unknown> | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (config && local === null) setLocal({ ...config });
  }, [config, local]);

  const dirty = useMemo(() => {
    if (!local || !config) return false;
    return JSON.stringify(local) !== JSON.stringify(config);
  }, [local, config]);

  const set = (k: string, v: unknown) => setLocal((c) => ({ ...(c ?? {}), [k]: v }));

  const save = async () => {
    if (!local) return;
    setSaving(true);
    try {
      await api.saveConfig(local);
      // refresh the base truth so the dirty flag clears against what the
      // server now holds
      await qc.invalidateQueries({ queryKey: ["config"] });
      toast.success("Grading settings saved");
    } catch (e) {
      toast.error(String(e));
    } finally {
      setSaving(false);
    }
  };

  const discard = () => {
    if (config) setLocal({ ...config });
    toast("Changes discarded");
  };

  const resetDefaults = () => {
    const d = defaults as Record<string, unknown> | undefined;
    if (!d) return;
    setLocal((c) => {
      const next = { ...(c ?? {}) };
      for (const k of GRADING_KEYS) if (d[k] !== undefined) next[k] = d[k];
      return next;
    });
    toast("Grading checks reset to defaults — Save to apply");
  };

  const val = (k: string) => !!local?.[k];

  const [q, setQ] = useState("");
  const needle = q.trim().toLowerCase();
  const visible = (items: CheckDef[]) =>
    needle
      ? items.filter((it) => it.label.toLowerCase().includes(needle)
          || it.k.toLowerCase().includes(needle) || it.desc.toLowerCase().includes(needle))
      : items;

  const setBulk = (v: boolean) =>
    setLocal((c) => {
      const next = { ...(c ?? {}) };
      for (const k of GRADING_TOGGLES) next[k] = v;
      return next;
    });

  /** Named presets: a one-click starting point that is still fully editable. */
  const [preset, setPreset] = useState<"strict" | "balanced" | "relaxed">("balanced");
  const applyPreset = (name: "strict" | "balanced" | "relaxed") => {
    const d = defaults as Record<string, unknown> | undefined;
    if (!d) return;
    setPreset(name);
    const relaxedOff = [
      "grade_check_tag_spaces", "grade_check_lyrics_spaces", "grade_check_cue_spaces",
      "grade_check_cover_crop", "grade_check_lyrics_zero", "grade_check_tag_blank_lines",
      "grade_check_lyrics_blank_lines", "grade_check_cue_blank_lines",
      "grade_check_filename_case", "grade_check_ext_case", "grade_check_excess_tags",
      "grade_check_mb_links", "grade_check_rym_links",
      // content checks — nothing breaks if the library ships without them
      "grade_check_replaygain", "grade_check_album_description",
      "grade_check_artist_image", "grade_check_artist_description",
    ];
    setLocal((c) => {
      const next = { ...(c ?? {}) };
      for (const k of GRADING_KEYS) if (d[k] !== undefined) next[k] = d[k];
      if (name === "strict") for (const k of CHECK_KEYS) next[k] = true;
      else if (name === "relaxed") for (const k of relaxedOff) next[k] = false;
      return next;
    });
    toast(name === "balanced" ? "Defaults loaded — Save to apply" : `${name[0].toUpperCase() + name.slice(1)} preset loaded — Save to apply`);
  };

  const PRESETS: { id: "strict" | "balanced" | "relaxed"; label: string; title: string; icon: typeof ShieldCheck }[] = [
    { id: "strict", label: "Strict", title: "Enable every check", icon: ShieldCheck },
    { id: "balanced", label: "Balanced", title: "Factory defaults", icon: ToggleRight },
    { id: "relaxed", label: "Relaxed", title: "Only the essential checks", icon: SunMedium },
  ];

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        sticky
        icon={ClipboardCheck}
        title="Grading"
        subtitle="Everything that can count for or against a grade, checked per track, album, artist folder and file. Toggles take effect the next time the grader runs (any grade view or the Grade script)."
        actions={
          <>
            {dirty && <span className="text-[10px] font-mono text-amber-400/80">unsaved changes</span>}
            <ConfirmButton
              onConfirm={resetDefaults}
              confirmLabel="Reset checks"
              disabled={!defaults || saving}
              title="Restore factory defaults for every grading check"
              className="btn-ghost !py-1.5 text-xs tap"
            >
              <RotateCcw className="h-3.5 w-3.5" /> Reset to defaults
            </ConfirmButton>
            <button className="btn-ghost !py-1.5 text-xs tap" onClick={discard} disabled={!dirty || saving}>
              <RotateCcw className="h-3.5 w-3.5" /> Discard
            </button>
            <button className="btn-primary !py-1.5 text-xs tap" onClick={save} disabled={!dirty || saving}>
              <Save className="h-3.5 w-3.5" /> {saving ? "Saving…" : "Save"}
            </button>
          </>
        }
      >
        {local && (
          <div className="flex items-center gap-2 flex-wrap">
            <input
              className="input !py-1.5 text-xs w-full sm:max-w-xs tap"
              placeholder="Filter checks… (tags, lyrics, cover…)"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <Segmented
              value={preset}
              onChange={applyPreset}
              options={PRESETS}
            />
            <button className="btn-ghost !py-1 text-xs tap" onClick={() => setBulk(true)}>Enable all</button>
            <button className="btn-ghost !py-1 text-xs tap" onClick={() => setBulk(false)}>Disable all</button>
            <span
              className="chip font-mono bg-white/5 border border-border text-zinc-400"
              title="Enabled grading checks — the file-category permissions are counted per group instead"
            >
              {CHECK_KEYS.filter(val).length}/{CHECK_KEYS.length} checks on
            </span>
          </div>
        )}
      </PageHeader>

      {!local ? (
        <div className="panel text-sm text-zinc-400 space-y-2">
          {configError ? (
            <>
              <div className="text-amber-300">
                Could not load the grading settings — the server may be restarting.
              </div>
              <button className="btn-ghost !py-1.5 text-xs tap" onClick={() => refetchConfig()}>
                <RefreshCw className="h-3.5 w-3.5" /> Retry
              </button>
            </>
          ) : (
            <PageLoading label="Loading grading settings…" />
          )}
        </div>
      ) : (
        GROUPS.map((g) => {
          const rows = visible(g.items);
          const on = g.items.filter((it) => val(it.k)).length;
          const tot = g.items.length;
          return (
            <section key={g.id} className="space-y-1.5">
              <div className="px-1 pt-2 flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-sm font-semibold">{g.title}</div>
                  <div className="text-[11px] text-zinc-500">{g.desc}</div>
                </div>
                <span
                  className={`chip font-mono shrink-0 mt-0.5 border ${
                    tot > 0 && on === tot
                      ? "bg-accent/10 border-accent/25 text-accent-soft"
                      : on === 0
                        ? "bg-white/5 border-border text-zinc-500"
                        : "bg-white/5 border-border text-zinc-400"
                  }`}
                  title={`${on} of ${tot} checks enabled in this group`}
                >
                  {on}/{tot}
                </span>
              </div>
              {g.id === "artist" && (
                <div className="px-1">
                  <div className="rounded-lg border border-accent/25 bg-accent/[0.06] px-3.5 py-2 text-[11px] leading-relaxed text-zinc-400">
                    <span className="font-semibold text-accent-soft">Artist grading.</span>{" "}
                    These checks run once per ARTIST folder and feed the artist page's own badge —
                    only the artist image and the artist description apply there. Every other check
                    on this page grades the albums inside the folder, so an artist can pass while one
                    of its albums fails, and the other way round.
                  </div>
                </div>
              )}
              <div className="stagger divide-y divide-border/40 rounded-lg border border-border/60 bg-panel/40">
                {rows.map((it) => (
                  <label
                    key={it.k}
                    className="flex items-start gap-3 px-3.5 py-2.5 cursor-pointer select-none hover:bg-raise/40 transition-colors"
                  >
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={val(it.k)}
                      onChange={(e) => set(it.k, e.target.checked)}
                    />
                    <span className="min-w-0">
                      <span className="text-sm text-zinc-200 block">{it.label}</span>
                      <span className="text-[11px] text-zinc-500 block leading-snug">{it.desc}</span>
                    </span>
                  </label>
                ))}
                {rows.length === 0 && (
                  <div className="px-3.5 py-3 text-xs text-zinc-500">
                    No checks in this group match “{q.trim()}”.
                  </div>
                )}
                {g.id === "auditing" &&
                  NUMBERS.map((n) => (
                    <div key={n.k} className="flex items-start gap-3 px-3.5 py-2.5">
                      <span className="min-w-0 flex-1">
                        <span className="text-sm text-zinc-200 block">{n.label}</span>
                        <span className="text-[11px] text-zinc-500 block leading-snug">{n.desc}</span>
                      </span>
                      <input
                        className="input !w-20 !py-1 text-sm shrink-0 tap"
                        type="number"
                        min={n.min}
                        max={n.max}
                        value={Number(local[n.k] ?? 0)}
                        onChange={(e) => set(n.k, Math.max(n.min, Math.min(n.max, Number(e.target.value) || 0)))}
                      />
                    </div>
                  ))}
              </div>
            </section>
          );
        })
      )}
    </div>
  );
}
