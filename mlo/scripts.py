"""The ONE script table: id -> (name, what the script does).

Every surface reads this list — the terminal menu (`mlo.cli`), the API's
`server.script_menu`, the stack builder (`server.api_stack`), the tag
registry's writer column (`server.tags_registry`) — so no menu can claim a
number means something another one does not. Names must equal
`server/script_runners.py` `RUNNERS` and `web/src/lib/scripts.ts`.

A LEAF on purpose (stdlib only, imports nothing from the engine): the tables
used to live in `mlo.cli`, whose module scope imports every script module
(artistdata, audit, autotag, flac, grader, images, loudness, lyrics, …), so
`server.tags_registry` — imported by `server.integrations` and therefore by the
whole API — dragged mutagen, Pillow, numpy and the rest of the engine into
every process start for a dict of strings.
"""

SCRIPTS = (
    (1, "Format lyrics", "multi-format + MEDIA/SOURCE normalization"),
    (2, "Format CUEs", "CD-N rename + FILE/INDEX layout"),
    (3, "Optimize FLACs", "lossless re-encode"),
    (4, "Grade", "per-album tag/lyrics/cover report"),
    (5, "Process images", "JXL / lossless / JXL-back"),
    (6, "Audit library", "AudioAuditor: fake lossless / upscaled / MQA"),
    (7, "DR & ReplayGain", "in-process DR + rsgain ReplayGain tags"),
    (8, "Auto tagging", "advisory / instrumental / mood / energy / genre"),
    (9, "AccurateRip", "CUETools .accurip files"),
    (10, "Format all", "final pass: .accurip / .cue / .lrc / tags"),
    (11, "Remux videos (MKV)", "any video -> MKV, audio -> FLAC"),
    (12, "Key & BPM", "musical key + tempo tags"),
    (13, "Fetch lyrics", "LRCLIB synced/plain"),
    (14, "Beets tagging", "MusicBrainz via beets"),
    (15, "Release tracklist", ".mlo_expected.json manifests"),
    (16, "Mood & Energy", "MOOD/ENERGY from the track's audio"),
    (17, "Lyrics transliterate (AI)", "TRANSLITERATION/TRANSLATION tags + sidecars"),
    (19, "Optimize artist images", "crop/resize artist artwork to the configured aspect and size"),
    (20, "Optimize library layout", "layout report + fixes (case, loose audio, empty artist, strays to the Trash)"),
    (21, "Fix AcoustID pairs", "complete or create ACOUSTID_ID / ACOUSTID_FINGERPRINT pairs"),
    (22, "Submit fingerprints (AcoustID)",
     "give AcoustID the fingerprint + MusicBrainz recording each track states"),
    (23, "Optimize tags",
     "delete excess tags: junk names, a valued COMMENT, unneeded aliases"),
    (24, "Web ratings",
     "aggregated public album + track scores (MusicBrainz / RYM / Discogs)"),
)
SCRIPT_LABELS = {sid: name for sid, name, _ in SCRIPTS}

# Scripts whose feature has its own on/off switch (mirror of the server's
# _DISABLED): with the switch off the runner is a no-op at best, so the CLI
# skips the script instead of reporting an empty run.
SCRIPT_GATES = {7: "dr_replaygain_enabled", 12: "audiometa_enabled",
                16: "mood_enabled", 17: ("lyrics_xlit_enabled", "lyrics_translate_enabled"),
                21: "acoustid_enabled",
                22: "acoustid_enabled", 23: "strip_unknown_tags",
                24: "web_ratings_enabled"}
