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
    (15, "Release tracklist", ".mb_expected manifests"),
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
     "aggregated public album + track scores (MusicBrainz)"),
    # 25/26 walk the artist folders and fetch only what is missing — the same
    # fetch the import's metadata step makes, for a library that arrived
    # another way. Two scripts because artist_image_enabled and
    # artist_description_enabled are separate switches: a user may want one and
    # not the other. Force = OVERWRITE (spec R379).
    (25, "Fetch artist images",
     "missing artist photos from the configured provider chain"),
    (26, "Fetch artist descriptions",
     "missing artist biographies from the configured provider chain"),
)
SCRIPT_LABELS = {sid: name for sid, name, _ in SCRIPTS}

# What each script needs FINISHED before it may START — the DATA edges of a
# chain, declared here beside the scripts themselves.
#
# A chain's order (mlo.config.DEFAULT_RUN_ALL_ORDER, or a saved
# `import_scripts` list) is still the run's order: the scheduler takes the
# EARLIEST ready step, so a declared edge only ever DELAYS a script, never
# moves one forward. An edge is declared when a script reads what another one
# WROTE, and only then:
#
#   8  (Auto tagging) — its advisory decision reads the track's own LYRICS
#        (mlo/advisory.py `_lyrics_text`, `mlo/autotag.py:1637`). The owner's
#        rule: advisory waits for lyrics, because the words decide the rating.
#   15 (Release tracklist) — the manifest is built from the MUSICBRAINZ_* ids
#        beets (14) wrote; without them it has nothing to ask about.
#   17 (Lyrics transliterate) — it transforms the text step 13 stored.
#   9  (AccurateRip) — its ArCueDotNet pass reads the CD-N .cue names step 2
#        settled.
#   6  (Audit library) — its AccurateRip leg reads the .accurip step 9 wrote.
#   4  (Grade) — it grades what the rest wrote (.mb_expected, xlit sidecars,
#        .accurip, AUDIT, cover, the ids), so it waits for all of them.
#
# An id the run does not contain is not a wait (a configured chain that leaves
# a step out is satisfied by definition), so an edge can never wedge a run.
SCRIPT_DEPS = {
    4: (1, 2, 5, 8, 9, 10, 13, 15, 17, 21, 23),
    6: (9,),
    # "advisories" is not a script: it is the step an IMPORT adds to its chain
    # (`run_chain`'s `extra`, built in server/imports.py) — the per-track
    # advisory fetch, which script 8 then derives the album tag from. It is
    # absent from a Run All, and a dependency naming an absent step is no wait.
    8: (13, "advisories"),
    9: (2,),
    15: (14,),
    17: (13,),
}

# Which LANE a script runs in.
#
# "album" (the default) is the run's spine: the scripts that read or write the
# album's own files or folder, run ONE AT A TIME. That is not a preference —
# a tag writer loads a whole container, changes it and writes it back
# (`mlo.audio.AudioFile` + `mlo.atomic`), so two of them on one file lose one
# side's writes.
#
# "free" is for scripts whose work is somewhere else entirely: the artist
# folder (19/25/26) or nothing on disk at all (22 submits to AcoustID and keeps
# its own ledger). They touch no album file, so they run CONCURRENTLY with the
# album lane — an import's artist photos and biographies are fetched while the
# album's own chain is still working, instead of after it.
#
# MEASURED constraint, not a guess: 8 and 13 both write tags to the same files
# (mlo/autotag.py:1678 holds a file under defer_save for a whole album pass),
# which is why they are one lane and their ordering is SCRIPT_DEPS' business.
SCRIPT_LANES = {19: "free", 22: "free", 25: "free", 26: "free"}

# Scripts whose feature has its own on/off switch (mirror of the server's
# _DISABLED): with the switch off the runner is a no-op at best, so the CLI
# skips the script instead of reporting an empty run.
SCRIPT_GATES = {7: "dr_replaygain_enabled", 12: "audiometa_enabled",
                16: "mood_enabled", 17: ("lyrics_xlit_enabled", "lyrics_translate_enabled"),
                21: "acoustid_enabled",
                22: "acoustid_enabled", 23: "strip_unknown_tags",
                24: "web_ratings_enabled",
                # 25/26 fetch what the import's metadata step fetches, and
                # these two keys are that step's own gates.
                25: "artist_image_enabled", 26: "artist_description_enabled"}
