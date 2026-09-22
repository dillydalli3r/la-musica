import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ClipboardCheck, RefreshCw, RotateCcw, Save, ShieldCheck, SunMedium, ToggleRight } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import ConfirmButton from "../components/ConfirmButton";
import Segmented from "../components/Segmented";
import { PageLoading } from "../components/Badges";
import PageHeader from "../components/PageHeader";
import { tagLabel, useTagRegistry } from "../lib/tags";
import type { TagRegistry } from "../lib/tags";

/** In-depth grading configuration: every check that can count for or
 * against grading, grouped the way they apply — track/album checks,
 * auditing, links, covers, strict formatting, lyrics & translations and
 * the file categories that participate in album grading at all. Toggles
 * edit a local copy; Save writes the whole grading block via saveConfig. */

interface Group {
  id: string;
  title: string;
  desc: string;
  /** The registry check keys this group shows, in the order it wants them. */
  keys: string[];
}

const GROUPS: Group[] = [
  {
    id: "tracks",
    title: "Tracks & albums",
    desc: "Core checks applied to every track and album in the library.",
    keys: [
      "grade_check_unreadable",
      "grade_check_missing_tags",
      "grade_check_album_tags",
      "grade_check_mood",
      "grade_check_energy",
      "grade_check_genre",
      "grade_check_genre_count",
      "grade_check_genre_order",
      "grade_check_genre_vocab",
      "grade_check_replaygain",
      "grade_check_encoder",
      "grade_check_naming",
      "grade_check_filename_case",
      "grade_check_ext_case",
      "grade_check_key_bpm",
      "grade_check_acoustid",
      "grade_check_excess_tags",
      "grade_check_media",
      "grade_check_source",
      "grade_check_instrumental",
      "grade_check_disallowed",
      "grade_check_extra_images",
      "grade_check_empty_folders",
      "grade_check_expected_tracks",
      "grade_check_album_description",
      "grade_check_raw_video",
      "grade_check_lossless_source",
      "grade_check_disc_naming",
      "grade_check_cd_log",
      "grade_check_cd_cue",
      "grade_check_cd_format",
      "grade_check_crc",
    ],
  },
  {
    id: "artist",
    title: "Artist",
    desc: "Graded once per ARTIST folder, not per album — the artist page shows the same two checks and its own badge.",
    keys: [
      "grade_check_artist_image",
      "grade_check_artist_description",
    ],
  },
  {
    id: "auditing",
    title: "Auditing",
    desc: "Audio verification (script 6) and the log scores it produces. With 'Require audit tag' on, AUDIT must read REAL — a FAKE or MIX verdict fails the track; off, the verdict does not change the grade. AccurateRip is AUDIT-ONLY: a .accurip that mismatches the reference database (or is missing) never fails grading on its own — it turns the AUDIT column FAKE, which is what fails the album once 'Require audit tag' is on.",
    keys: [
      "grade_check_audit",
      "grade_check_log_checksum",
      "grade_check_accuraterip",
      "grade_check_log_grade",
    ],
  },
  {
    id: "links",
    title: "Identity links",
    desc: "The two release-level identity links, graded per track.",
    keys: [
      "grade_check_mb_links",
      "grade_check_rym_links",
    ],
  },
  {
    id: "covers",
    title: "Covers",
    desc: "Cover art presence and the size / square rules from Settings → Images.",
    keys: [
      "grade_check_cover",
      "grade_check_cover_crop",
      "grade_check_sidecar_cover",
    ],
  },
  {
    id: "formatting",
    title: "Strict formatting",
    desc: "Whitespace / blank-line / canonical-form rules. These make near-miss files fail so the formatter scripts can fix them.",
    keys: [
      "grade_check_tag_spaces",
      "grade_check_tag_case",
      "grade_check_tag_blank_lines",
      "grade_check_lyrics_spaces",
      "grade_check_lyrics_blank_lines",
      "grade_check_lyrics_zero",
      "grade_check_lyrics_format",
      "grade_check_cue_spaces",
      "grade_check_cue_blank_lines",
      "grade_check_cue_format",
      "grade_check_accurip_format",
      "grade_check_cue_files",
    ],
  },
  {
    id: "lyrics",
    title: "Lyrics & translations",
    desc: "Presence checks for lyrics and the translation/transliteration tags stored beside them (TRANSLATION-EN, TRANSLITERATION-JA-LATN, sidecars).",
    keys: [
      "grade_check_lyrics",
      "grade_check_lyrics_lang_tags",
      "grade_check_xlit_transliteration",
      "grade_check_xlit_translation",
    ],
  },
  {
    id: "categories",
    title: "File categories",
    desc: "Which file types participate in album grading. Turning a category off stops its own checks AND marks those files as disallowed — the 'Disallowed file types' toggle above decides whether that fails the album.",
    keys: [
      "grade_include_music",
      "grade_include_cover",
      "grade_include_description",
      "grade_include_cue",
      "grade_include_log",
      "grade_include_lrc",
      "grade_include_accurip",
      "grade_include_video",
      "grade_include_other",
    ],
  },
];

/** What each check means, keyed by its registry key.
 *
 *  Prose only: WHICH checks exist is the registry's answer
 *  (server/tags_registry.py reads them straight out of DEFAULT_CONFIG), and
 *  this page renders a row per registry check. A check this map does not
 *  describe still renders — with its registry label and the tags it grades —
 *  so a new check can never be invisible here, which is how
 *  grade_check_genre_vocab once went missing from Settings. */
const CHECK_DESC: Record<string, string> = {
  grade_check_unreadable: "Files that can't be opened or decoded fail the album.",
  grade_check_missing_tags: "Every required per-track tag (title, artist, date, …) must exist and be non-empty.",
  grade_check_album_tags: "Album-wide tags (album, album artist, catalog number, …) must be present on the tracks.",
  grade_check_mood: "Every track needs a MOOD tag — script 8 fills it (script 16 re-runs just that classifier), so no track should ship without one (issue code MOOD_MISSING).",
  grade_check_energy: "Every track needs an ENERGY tag (0-100, written with MOOD by script 8 or 16; issue code ENERGY_MISSING).",
  grade_check_genre: "Every track needs a GENRE tag. Graded on its own, independent of the required-tags sweep (issue code GENRE_MISSING).",
  grade_check_genre_count: "A track may hold AT MOST the number of genres set by 'Genres per track' in Settings → Import & tags (mb_genre_count) — only an overflow fails (issue code GENRE_COUNT). Fewer is fine: the family is derived from the specific genre, so one specific genre is a complete answer and nothing is topped up with filler.",
  grade_check_genre_order: "The family, if present, must be the FIRST genre — Rock / Shoegaze. A family in a later slot, or a genre repeated, fails (issue code GENRE_ORDER). The names themselves are graded by the vocabulary check below.",
  grade_check_genre_vocab: "Every GENRE name must be one MusicBrainz publishes (shoegaze, dream pop, …). A name it does not know fails with issue code GENRE_VOCAB and is named in the report — the writers keep what a source said, so grading is where it surfaces. Grading never rewrites the tag: run Auto tagging (8) or Format all (10) to canonicalize it.",
  grade_check_replaygain: "A file that carries any REPLAYGAIN_* tag must carry all four — REPLAYGAIN_TRACK_GAIN/_PEAK and _ALBUM_GAIN/_PEAK. A file with none is not graded (run the Loudness pass; the player can also analyse on demand).",
  grade_check_encoder: "The ENCODER_* markers switched on under Tagging → Encoder tags must be present (PROGRAM is off by default). Covers are graded by the same rule while image processing is on.",
  grade_check_naming: "File paths must match the configured naming script (full or shortened MusicBrainz IDs both accepted).",
  grade_check_filename_case: "Filenames and folder names must match the naming script's letter case exactly — TOXICITY vs Toxicity fails. Organize applies the canonical casing.",
  grade_check_ext_case: "File extensions must be lowercase (01 - Song.FLAC fails). Organize lowercases every extension it touches.",
  grade_check_key_bpm: "INITIALKEY and BPM tags (written by script 12) are required.",
  grade_check_acoustid: "Files already carrying ACOUSTID_ID or ACOUSTID_FINGERPRINT must keep both — a library without them is never graded.",
  grade_check_excess_tags: "Any tag the optimizer would strip — outside the known tag set — fails the track. Run Optimization to remove them.",
  grade_check_media: "The MEDIA tag must be present and consistent with the release.",
  grade_check_source: "The SOURCE tag must be present (with different rules for CD vs digital releases).",
  grade_check_instrumental: "INSTRUMENTAL=1 tracks must not carry lyrics; INSTRUMENTAL=0 tracks are graded for lyrics below.",
  grade_check_disallowed: "Unclassified files (.txt, .pdf, .m3u, …) fail the album unless their category is enabled under File categories.",
  grade_check_extra_images: "Images that are neither cover.* nor per-track sidecars fail the album.",
  grade_check_empty_folders: "A folder with no audio track anywhere beneath it fails the run (issue code EMPTY_FOLDER) — albums come from audio files, so such a folder would otherwise be skipped silently. A folder still holding part of the album (cover.*, .cue, .log, .lrc, .accurip) counts too: its audio is gone. Hidden and app-state folders are ignored.",
  grade_check_expected_tracks: "An album that carries a MusicBrainz release id but no .mlo_expected.json fails the run (issue code EXPECTED_TRACKS_MISSING). The manifest records the release's own tracklist, which is the only way a partial import can name the tracks that never arrived; script 15 (Release tracklist) writes it, and the album page greys out the missing tracks from it. An album with no release id is not graded on it — script 15 writes no manifest without one, so the check could never be cleared.",
  grade_check_album_description: "The album folder needs a non-blank description.txt — fetch one on the album page.",
  grade_check_raw_video: "Un-remuxed videos (VOB/AVI/WMV/TS) fail — run script 11 to normalize them to MKV.",
  grade_check_lossless_source: "Uncompressed lossless sources (WAV/AIFF/APE/WV/SHN) fail — script 3 converts them to FLAC.",
  grade_check_disc_naming: "A CD's .log / .cue / .accurip files must follow the configured disc pattern (CD-1, CD-2 … by default). The check runs whatever auto-rename is set to — with it off, renaming them is a manual job (the issue text says so). Disc FOLDERS are the layout scan's business: it reports a folder inside an album that is not a disc folder, and the naming script covers their letter case.",
  grade_check_cd_log: "Every CD disc needs an exact-match .log file.",
  grade_check_cd_cue: "Every CD disc needs a .cue sheet.",
  grade_check_cd_format: "CD tracks must be FLAC (lossless).",
  grade_check_crc: "Every track must be covered by a per-track CRC in its own disc's .log, and that CRC must match the CRC of the track's decoded audio — coverage alone is not enough (issue codes CRC / CRC_MISMATCH).",
  grade_check_artist_image: "The artist folder must hold an artist.jpg / artist.png that decodes, matches the configured artist_image_aspect (±2%) and stays under the artist_image_target_size ceiling (issue codes ARTIST_IMAGE_MISSING / _CORRUPT / _FORMAT / _OVERSIZED / _ASPECT / _UPSCALED). An image below the target is reported as a note and passes — nothing here upscales. Script 19 (Optimize artist images) fixes every one of them.",
  grade_check_artist_description: "The artist folder must hold a non-blank description.txt (issue code ARTIST_DESCRIPTION_MISSING).",
  grade_check_audit: "Tracks must carry an AUDIT tag (run Audit Library). ON by default — the verdict is the rip's OWN evidence (the .log's per-track CRC against the decoded audio, then AccurateRip), never a spectral guess, so it fails only a disc nothing verified. Off, an unaudited library is never failed for the tag.",
  grade_check_log_checksum: "A log checksum that IS PRESENT must verify. One that is absent is not required and costs nothing: XLD, EAC before v1.0 and a 1.0+ log whose 'Log checksum' line is gone are all judged by their per-track CRCs (issue code LOG_CHECKSUM fires only on a checksum that was there and did not verify).",
  grade_check_accuraterip: "A .accurip whose verdict is not REAL marks the album's AUDIT FAKE (and each affected track red) — grading itself is reserved to tagging, so this key never costs a grade point. Turn on 'Require audit tag' for that verdict to fail the album.",
  grade_check_log_grade: "LOG_GRADE tag must exist and be 0–100.",
  grade_check_mb_links: "The MusicBrainz release (or its release group) must be tagged.",
  grade_check_rym_links: "The RateYourMusic release page URL must be tagged.",
  grade_check_cover: "The album must have cover art meeting the configured size, squareness and crop rules.",
  grade_check_cover_crop: "The cover's width/height must be square within cover_crop_threshold — an aspect-ratio test, not crop detection (issue: 'Cover aspect ratio WxH not square').",
  grade_check_sidecar_cover: "Sidecar covers (01 - Song.jpg) must meet the same cover rules.",
  grade_check_tag_spaces: "Leading/trailing spaces or tabs in any tag value fail, and so does a run of two or more internal spaces — script 10 collapses both.",
  grade_check_tag_case: "A tag whose value has a canonical spelling must be stored that way: MEDIA (CD, Digital Media), SOURCE, RELEASETYPE (Album; Live), RELEASESTATUS, AUDIT, RELEASECOUNTRY, SCRIPT and MOOD (Happy). A vocabulary's unknown value — a mood you typed, a release type MusicBrainz does not publish — is left alone, and free text (TITLE, ALBUM, ARTIST, LABEL, …) is never touched, which is what keeps AC/DC and k.d. lang intact. One check per track (issue code TAG_CASE); run Format all (script 10) to fix a library.",
  grade_check_tag_blank_lines: "Blank lines inside tag values fail (LYRICS is exempt — its own rules apply).",
  grade_check_lyrics_spaces: "Leading/trailing spaces on lyric lines fail.",
  grade_check_lyrics_blank_lines: "Blank-line placement must match the lyrics formatter's canonical output.",
  grade_check_lyrics_zero: "The [00:00.00] leader line must follow the configured lyrics rules.",
  grade_check_lyrics_format: "Stored lyrics must exactly match what the Format Lyrics script would produce.",
  grade_check_cue_spaces: "Leading/trailing spaces in CUE lines fail.",
  grade_check_cue_blank_lines: "Blank lines in CUE sheets fail.",
  grade_check_cue_format: "CUE sheets must match the canonical formatter output.",
  grade_check_accurip_format: ".accurip files must match the canonical shape (each line trimmed, outer blank lines handled) that the AccurateRip / Format All scripts write — run one of them to fix it.",
  grade_check_cue_files: "Every file a CUE's FILE line names must exist in the album. The formatter carries names through verbatim, so a converted (wav→flac) or renamed album keeps a sheet pointing at a file that is not there (run the CUE Sheets script).",
  grade_check_lyrics: "Every non-instrumental track needs lyrics (embedded and/or .lrc sidecar, per the lyrics format).",
  grade_check_lyrics_lang_tags: "Transform tags must carry their language (TRANSLATION-EN, TRANSLITERATION-JA-LATN — never the bare legacy names).",
  grade_check_xlit_transliteration: "A track whose lyrics are already Latin script must NOT carry a TRANSLITERATION tag (or a .romaji.lrc sidecar) — that fails as XLIT_UNNEEDED — while non-Latin lyrics must have one, or it fails as XLIT_MISSING. Instrumental tracks are never graded on it.",
  grade_check_xlit_translation: "Computed against the reader's language (the first of 'Lyrics translation languages'): a track already in that language must not carry a TRANSLATION tag or a .<lang>.lrc sidecar (XLIT_UNNEEDED), and one that is not must have the right one (XLIT_MISSING, which also names a mismatch like TRANSLATION-DE stored for English lyrics).",
  grade_include_music: "The music files themselves.",
  grade_include_cover: "cover.* images count toward the grade.",
  grade_include_description: "description.txt (fetched on the album page) counts as the app's own file rather than a stray one.",
  grade_include_cue: ".cue sidecars count toward the grade.",
  grade_include_log: ".log sidecars count toward the grade.",
  grade_include_lrc: ".lrc sidecars count toward the grade.",
  grade_include_accurip: ".accurip files count toward the grade.",
  grade_include_video: "MKV/MP4 music videos count toward the grade.",
  grade_include_other: "Unclassified files (.txt/.pdf/.m3u, …) are allowed to sit in an album folder. Nothing else grades them — the layout scan (script 20) is what reports them as stray files. Turn this off and one fails the album instead (Disallowed file types).",
};

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

/** Every grading key this page owns — the reset-to-defaults scope.
 *
 *  The keys come from the REGISTRY (which reads them out of DEFAULT_CONFIG),
 *  not from the groups: a check the config holds and no group lists is still
 *  reset, and it still renders (see the "Other checks" section). */
const GRADING_KEYS = (reg: TagRegistry | undefined) => [
  ...(reg?.checks.map((c) => c.key) ?? []),
  ...NUMBERS.map((n) => n.k),
];

/** The same list WITHOUT the numeric settings: Enable/Disable all may only
 *  write booleans. Writing them over grade_log_score_threshold turned a tuned
 *  threshold into `false` (rendered as 0, which disables the check). */
const GRADING_TOGGLES = (reg: TagRegistry | undefined) => reg?.checks.map((c) => c.key) ?? [];

/** The real toggles, minus the file-category permissions: a preset may force
 *  checks on, but choosing what counts toward a grade is the user's call. */
const CHECK_KEYS = (reg: TagRegistry | undefined) =>
  (reg?.checks ?? []).filter((c) => !c.key.startsWith("grade_include_")).map((c) => c.key);

/** What the Balanced preset puts BACK: the grading keys 3.7.0 turned on when
 *  it made grading strict by default (the audit-tag requirement and the
 *  "other" file category). Balanced is therefore the factory defaults as an
 *  install had them BEFORE that — the same list, one name apart — and NOT a
 *  second hand-kept copy of the sixty defaults it shares with Strict.
 *
 *  `server.api_stack.BALANCED_OFF` is the server's half of the same list and
 *  is what /api/stack reports per check; tools/test_check_stack.py fails if
 *  the two ever disagree, exactly as it does for `relaxedOff` below. */
const balancedOff = [
  "grade_check_audit",
  "grade_include_other",
];

/** The registry checks one group shows, in registry order. */
const groupChecks = (
  reg: TagRegistry | undefined,
  keys: string[],
): { key: string; label: string; desc: string; tags: string[] }[] =>
  (reg?.checks ?? [])
    .filter((c) => keys.includes(c.key))
    .map((c) => ({ key: c.key, label: c.label, desc: CHECK_DESC[c.key] ?? "", tags: c.tags }));

export default function GradingPage() {
  const { data: config, isError: configError, refetch: refetchConfig } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
  });
  const { data: defaults } = useQuery({ queryKey: ["configDefaults"], queryFn: api.configDefaults });
  const reg = useTagRegistry();
  const qc = useQueryClient();
  const [local, setLocal] = useState<Record<string, unknown> | null>(null);
  const [saving, setSaving] = useState(false);

  // Checks the registry holds that no group above lists. They are rendered in
  // their own section instead of being dropped: the registry reads the check
  // list out of DEFAULT_CONFIG, so a check added to the grader shows up here
  // the day it exists, without this file being touched.
  const orphanKeys = (reg?.checks ?? [])
    .map((c) => c.key)
    .filter((k) => !GROUPS.some((g) => g.keys.includes(k)));
  const sections: Group[] = orphanKeys.length
    ? [...GROUPS, { id: "other", title: "Other checks", desc: "Graded checks this page has no group for yet — they come straight from the grader's own config keys.", keys: orphanKeys }]
    : GROUPS;

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
      for (const k of GRADING_KEYS(reg)) if (d[k] !== undefined) next[k] = d[k];
      return next;
    });
    toast("Grading checks reset to defaults — Save to apply");
  };

  const val = (k: string) => !!local?.[k];

  const [q, setQ] = useState("");
  const needle = q.trim().toLowerCase();
  const visible = <T extends { key: string; label: string; desc: string }>(items: T[]): T[] =>
    needle
      ? items.filter((it) => it.label.toLowerCase().includes(needle)
          || it.key.toLowerCase().includes(needle) || it.desc.toLowerCase().includes(needle))
      : items;

  const setBulk = (v: boolean) =>
    setLocal((c) => {
      const next = { ...(c ?? {}) };
      for (const k of GRADING_TOGGLES(reg)) next[k] = v;
      return next;
    });

  /** Named presets: a one-click starting point that is still fully editable.
   *  The control starts on Strict because that IS what a fresh install's
   *  config holds (mlo/config.py ships every check on); it tracks the last
   *  preset clicked after that. */
  const [preset, setPreset] = useState<"strict" | "balanced" | "relaxed">("strict");
  const applyPreset = (name: "strict" | "balanced" | "relaxed") => {
    const d = defaults as Record<string, unknown> | undefined;
    if (!d) return;
    setPreset(name);
    const relaxedOff = [
      "grade_check_tag_spaces", "grade_check_lyrics_spaces", "grade_check_cue_spaces",
      "grade_check_cover_crop", "grade_check_lyrics_zero", "grade_check_tag_blank_lines",
      "grade_check_tag_case",
      "grade_check_lyrics_blank_lines", "grade_check_cue_blank_lines",
      "grade_check_filename_case", "grade_check_ext_case", "grade_check_excess_tags",
      "grade_check_mb_links", "grade_check_rym_links",
      // content checks — nothing breaks if the library ships without them
      "grade_check_replaygain", "grade_check_album_description",
      "grade_check_artist_image", "grade_check_artist_description",
    ];
    setLocal((c) => {
      const next = { ...(c ?? {}) };
      for (const k of GRADING_KEYS(reg)) if (d[k] !== undefined) next[k] = d[k];
      if (name === "strict") for (const k of CHECK_KEYS(reg)) next[k] = true;
      else for (const k of name === "balanced" ? balancedOff : relaxedOff) next[k] = false;
      return next;
    });
    toast(`${name[0].toUpperCase() + name.slice(1)} preset loaded — Save to apply`);
  };

  const PRESETS: { id: "strict" | "balanced" | "relaxed"; label: string; title: string; icon: typeof ShieldCheck }[] = [
    { id: "strict", label: "Strict", title: "Every check on — what a fresh install ships", icon: ShieldCheck },
    { id: "balanced", label: "Balanced", title: "The factory defaults as they were before 3.7.0 (audit tag not required)", icon: ToggleRight },
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
              {CHECK_KEYS(reg).filter(val).length}/{CHECK_KEYS(reg).length} checks on
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
        sections.map((g) => {
          const group = groupChecks(reg, g.keys);
          const rows = visible(group);
          const on = group.filter((it) => val(it.key)).length;
          const tot = group.length;
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
                    key={it.key}
                    className="flex items-start gap-3 px-3.5 py-2.5 cursor-pointer select-none hover:bg-raise/40 transition-colors"
                  >
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={val(it.key)}
                      onChange={(e) => set(it.key, e.target.checked)}
                    />
                    <span className="min-w-0">
                      <span className="text-sm text-zinc-200 block">{it.label}</span>
                      {/* The prose is this page's; the checks it describes are
                          the registry's. A check with no prose yet still shows
                          — its name and the tags it grades. */}
                      <span className="text-[11px] text-zinc-500 block leading-snug">
                        {it.desc || (it.tags.length
                          ? `Grades: ${it.tags.map((t) => tagLabel(reg, t)).join(", ")}`
                          : it.key)}
                      </span>
                      {it.desc && it.tags.length > 0 && (
                        <span className="text-[10px] text-zinc-600 block leading-snug font-mono">
                          {it.tags.join(" · ")}
                        </span>
                      )}
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
