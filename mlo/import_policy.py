"""Import autonomy: what an import decides on its own, and what it hands over.

Every import path ends in ``server.imports.finish_album``, and the question
this module answers for it is the one the silence used to hide: an album that
lands in the library half-tagged is a grading failure nobody was told about.

Three things are decided here, each in ONE place, because the pipeline, the
config validator, the wizard and the notification all need the same answer:

* **mode** — ``import_autonomy`` says whether an import runs to its end and
  then reports what is missing ("automatic", the default), or stops before the
  first step that needs a decision and hands the album over ("review").
* **families** — the things an album carries that no script can invent from the
  audio alone, in the wizard's own step order (Links, Covers, Genres, Lyrics,
  Advisory). ``import_review_families`` names the ones the user wants to decide
  by hand even in automatic mode; review mode keeps all of them.
* **manual counterparts** — every family entry carries the options a person
  uses instead (`manual_options`), one row per thing the step decides, each
  naming the route it calls, the entry point both halves share and the tags and
  sidecars it owns. The pipeline's own steps that no family owns (the artist's
  image, the two descriptions, the instrumental pass) are in `OTHER_STEPS`, in
  the same shape. The audit test reads that table: it is what keeps "an import
  can do it" and "a person can do it" from drifting apart.
* **gaps** — what an album is still missing once the pipeline has done all it
  can. Read off `mlo.grader`'s own checks — a gap here IS a grading failure,
  never a second opinion — plus the two families the grader deliberately does
  not require: a missing ITUNESADVISORY is "unrated" by design and a staged
  cover is a pick waiting to be made, so those two come from the steps' own
  results instead.

A family is one wizard step, and each entry names that step: it is what lets a
gap send the user to the exact place where the decision is made.

Two switches sit above all of that and are read here so every stage of the
pipeline asks the same question in the same place: ``auto_acquisition_enabled``
is the master switch for what the app does ON ITS OWN (the wishes worker
searching, an artist watch queueing, an add-to-library request downloading) and
``manual_import_enabled`` is the switch over the user's own import path (the
wizard and the ``POST /api/import/*`` routes). Both default on, and
``page_download_auto_import`` reads the two together: a download the user
queued from the Soulseek page imports itself only while an automatic import is
what this install wants at all.
"""

MODES = ("automatic", "review")

# Wizard order — the same order as the wizard's own steps (the Match step
# between Links and Covers is not a family: it decides which RELEASE the album
# is, and a release the import could not identify is reported by the family
# steps that need its identity). `off` is what "do not decide this for me"
# means to the step that reads it, `review_key` the switch a family may
# already be under review by, `chain` the script ids that would otherwise
# decide it, and `manual` the options a person uses instead — one row per
# thing the family carries, in the shape `manual_options` documents.
FAMILIES = (
    {
        "id": "links",
        "label": "Links",
        "step": "Links",
        "off": {"rym_links_auto": False},
        "codes": {"MB_LINK": "MusicBrainz release link",
                  "RYM_LINK": "RateYourMusic release link"},
        "manual": (
            {"id": "links-mb",
             "surface": ("wizard", "album-page", "track-page", "tag-actions"),
             "route": "/api/mb/assign",
             "method": "POST",
             "auto": "server.imports:_stamp_release",
             "service": "server.main:mb_assign",
             "tags": ("MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_RELEASEGROUPID",
                      "MUSICBRAINZ_ALBUMARTISTID", "MUSICBRAINZ_TRACKID",
                      "MUSICBRAINZ_RELEASETRACKID", "LABEL", "CATALOGNUMBER",
                      "BARCODE", "DATE", "ORIGINALDATE", "RELEASECOUNTRY",
                      "RELEASESTATUS", "RELEASETYPE", "MEDIA", "SCRIPT"),
             "files": ()},
            {"id": "links-rym",
             "surface": ("wizard", "links-editor"),
             "route": "/api/rym/resolve",
             "method": "GET",
             "auto": "server.imports:stamp_rym_links",
             "service": "server.main:rym_resolve",
             "tags": ("RATEYOURMUSIC_ALBUM", "RATEYOURMUSIC_ARTIST"),
             "files": ()},
        ),
    },
    {
        "id": "cover",
        "label": "Cover art",
        "step": "Covers",
        # Asking for the pick back means staging candidates instead of writing
        # the first hit — the switch the cover step already reads.
        "off": {"cover_review": True},
        "review_key": "cover_review",
        "codes": {"COVER": "cover art"},
        "manual": (
            {"id": "cover-file",
             "surface": ("wizard", "album-page", "track-page"),
             "route": "/api/cover",
             "method": "POST",
             "auto": "server.imports:run_cover_step",
             "service": "server.main:upload_cover",
             "tags": (),
             "files": ("cover.jpg", "cover.png")},
            {"id": "cover-online",
             "surface": ("wizard", "album-page", "tag-actions"),
             "route": "/api/cover/fromurl",
             "method": "POST",
             "auto": "server.imports:cover_candidates",
             "service": "server.main:cover_from_url",
             "tags": (),
             "files": ("cover.jpg", "cover.png")},
        ),
    },
    {
        "id": "genres",
        "label": "Genres",
        "step": "Genres",
        "off": {"genre_autofill": False},
        "codes": {"GENRE": "genre",
                  "GENRE_MISSING": "genre",
                  "GENRE_COUNT": "genre count",
                  "GENRE_ORDER": "genre order",
                  "GENRE_VOCAB": "genre vocabulary"},
        "manual": (
            {"id": "genres-all",
             "surface": ("wizard", "album-page", "tag-actions"),
             "route": "/api/genres/import",
             "method": "POST",
             "auto": "server.imports:_stamp_release",
             "service": "server.main:genres_import",
             "tags": ("GENRE",),
             "files": ()},
        ),
    },
    {
        "id": "lyrics",
        "label": "Lyrics",
        "step": "Lyrics",
        # The family is the whole lyrics chain (fetch → transliterate →
        # publish), so a lyrics review drops all of it from the chain rather
        # than letting a later script write what the user kept for themselves
        # (see `dropped_chain_ids`). No switch makes script 13 fetch, and
        # nothing gates 17 once lyrics exist.
        "chain": (13, 17),
        "codes": {"LYRICS": "lyrics",
                  "XLIT_MISSING": "lyric transliteration/translation"},
        # The three halves of the chain, each with the option that runs the
        # SAME entry point by hand — the fetch and the transliteration pass are
        # per-track cores the route and the script both call, and the publish
        # is the one core the editor's own route shares. `tags`/`files` are
        # what each half owns: the fetch and the transforms write local data,
        # publishing writes NOTHING locally (it is the only outward step).
        "manual": (
            {"id": "lyrics-fetch",
             "surface": ("wizard", "tag-actions", "album-page", "track-page"),
             "route": "/api/lyrics/auto",
             "method": "POST",
             "auto": "mlo.lyrics_fetch:fetch_one",
             "service": "mlo.lyrics_fetch:fetch_one",
             "tags": ("LYRICS",),           # USLT / ©lyr / vorbis LYRICS
             "files": (".lrc",)},
            {"id": "lyrics-xlit",
             "surface": ("wizard", "tag-actions"),
             "route": "/api/lyrics/xlit",
             "method": "POST",
             "auto": "mlo.lyrics_xlit:run_lyrics_xlit",
             "service": "mlo.lyrics_xlit:run_lyrics_xlit",
             "tags": ("TRANSLITERATION-<lang>", "TRANSLATION-<lang>"),
             "files": (".romaji.lrc", ".<lang>.lrc")},
            {"id": "lyrics-publish",
             "surface": ("wizard", "tag-actions", "track-page"),
             "route": "/api/lyrics/publish-batch",
             "method": "POST",
             "auto": "mlo.lyrics_publish:publish_one",
             "service": "mlo.lyrics_publish:publish_one",
             "tags": (),
             "files": ()},
        ),
    },
    {
        "id": "advisory",
        "label": "Advisory",
        "step": "Advisory",
        # "none" is the fallback that writes nothing at all: an advisory the
        # ladder was told not to invent is one the user answers.
        "off": {"advisory_auto_fetch": False, "advisory_fallback": "none"},
        "codes": {"ITUNESADVISORY": "advisory"},
        "manual": (
            {"id": "advisory-fetch",
             "surface": ("wizard", "tag-actions"),
             "route": "/api/mb/advisory/fetch",
             "method": "POST",
             "auto": "server.imports:fetch_advisories",
             "service": "server.main:mb_advisory_fetch",
             "tags": ("ITUNESADVISORY",),
             "files": ()},
        ),
    },
)


# The import's own steps that NO family owns: they are not decisions (the
# grader counts them, nothing hands them to the user as a gap), but they are
# still features an import performs, so a person must be able to run each one
# by hand — the same row shape as a family's `manual` tuple, and the same way of
# saying which tags it owns.
OTHER_STEPS = (
    {"id": "artist-image",
     "family": None,
     "surface": ("wizard", "artist-page", "tag-actions"),
     "route": "/api/artist/image",
     "method": "POST",
     "auto": "server.imports:apply_metadata",
     "service": "server.api_discovery:artist_image_save",
     "tags": (),
     "files": ("artist.jpg", "artist.png")},
    {"id": "artist-description",
     "family": None,
     "surface": ("wizard", "artist-page", "tag-actions"),
     "route": "/api/artist/description",
     "method": "POST",
     "auto": "server.imports:apply_metadata",
     "service": "server.api_discovery:artist_description_save",
     "tags": (),
     "files": ("description.txt",)},
    {"id": "album-description",
     "family": None,
     "surface": ("wizard", "album-page", "tag-actions"),
     "route": "/api/album/description",
     "method": "POST",
     "auto": "server.imports:apply_metadata",
     "service": "server.api_discovery:album_description_save",
     "tags": (),
     "files": ("description.txt",)},
    {"id": "instrumental",
     "family": None,
     "surface": ("wizard", "tag-actions"),
     "route": "/api/instrumental/fetch",
     "method": "POST",
     "auto": "server.imports:fetch_instrumentals",
     "service": "server.main:instrumental_fetch",
     "tags": ("INSTRUMENTAL",),
     "files": ()},
)

FAMILY_IDS = tuple(f["id"] for f in FAMILIES)

# What a manual row says, field by field. The point of the table is that the
# automatic path and the option a person clicks cannot drift apart silently, so
# every row carries BOTH halves:
#
#   id       stable key, also the action id a surface is keyed by
#   surface  where a user finds the option ("wizard", "tag-actions", the entity
#            pages, the links editor)
#   route    the HTTP route the option calls, and `method` the verb — the
#            app must serve that exact pair
#   service  `module:attr` the option calls (what the route's handler runs)
#   auto     `module:attr` of the automatic counterpart's entry point. Equal to
#            `service` where the button and the pipeline run ONE code path, which
#            is the strongest form of "the two must not diverge"
#   tags     the audio tags the step owns (a `<lang>` part is per-language)
#   files    the sidecar files it owns ("" = none, e.g. publishing to LRCLIB
#            writes nothing locally at all)
#
# `OTHER_STEPS` rows carry the same fields plus `family` (None there, since no
# family names them).
MANUAL_KEYS = ("id", "surface", "route", "method", "service", "auto", "tags", "files")


def manual_options(family_id=None):
    """The manual counterparts: every family's rows, or one family's.

    Without an argument: every row of every family plus `OTHER_STEPS`, in the
    wizard's step order — the whole "what an import does, and where a person
    does it instead" table.
    """
    if family_id is None:
        rows = []
        for entry in FAMILIES:
            for row in entry.get("manual") or ():
                rows.append({"family": entry["id"], **row})
        return tuple(rows) + tuple(OTHER_STEPS)
    entry = family(family_id)
    return tuple(entry.get("manual") or ()) if entry else ()


def manual_option(option_id):
    """One row by its id, across the families and `OTHER_STEPS`, or None."""
    for row in manual_options():
        if row["id"] == option_id:
            return row
    return None


def option_ids(family_id):
    """The manual option ids one family offers, in table order."""
    return tuple(row["id"] for row in manual_options(family_id))


_BY_ID = {f["id"]: f for f in FAMILIES}
_ORDER = {f["id"]: i for i, f in enumerate(FAMILIES)}


def family(family_id):
    """One family's entry, or None for a name the app does not know."""
    return _BY_ID.get(str(family_id or "").strip().lower())


def order_of(family_id):
    """Its position in the wizard's step order (len = unknown)."""
    return _ORDER.get(str(family_id or "").strip().lower(), len(FAMILIES))


def mode(cfg):
    """`import_autonomy` — "automatic" unless the config says "review".

    A config written before this key existed reads as automatic: the default
    is the whole point, and the validator fills the key in for a fresh one.
    """
    value = str((cfg or {}).get("import_autonomy") or "").strip().lower()
    return value if value in MODES else "automatic"


# What a stage says when its own switch is off. ONE sentence each, so the wish
# log, the watch's summary, the API's refusal and the UI all use the same
# words: who refused, why, and what to do about it.
AUTO_OFF_NOTE = (
    "Automatic acquisition is off (auto_acquisition_enabled) — nothing was "
    "searched or downloaded. What you asked for is recorded; turn the switch "
    "back on in Settings → Import pipeline, or start it by hand.")
MANUAL_OFF_NOTE = (
    "Importing by hand is off (manual_import_enabled) — turn it back on in "
    "Settings → Import pipeline to import albums yourself.")


def auto_acquisition_enabled(cfg):
    """`auto_acquisition_enabled` — may the app search and download on its own?

    The master switch over every unattended acquisition: the wishes worker's
    own passes, an artist watch queueing a release, and an "Add to library"
    request starting the download. A user's own action (a wish's Search now,
    the wizard, the Soulseek page) is not this switch's business.
    """
    return bool((cfg or {}).get("auto_acquisition_enabled", True))


def manual_import_enabled(cfg):
    """`manual_import_enabled` — may the user's own import path run?

    The wizard and the ``POST /api/import/*`` routes ask this before doing any
    work; off, they refuse with `MANUAL_OFF_NOTE` rather than importing.
    """
    return bool((cfg or {}).get("manual_import_enabled", True))


def page_download_auto_import(cfg):
    """May a download the user queued from the Soulseek page import itself?

    The page's own Download button queues transfers and used to leave the
    album sitting in the download folder until somebody pressed Import —
    a second decision for an act the user had already taken. It is imported by
    the app now, and this is the one question that decides whether it is: the
    two switches that say "a person decides this import" answer it, and BOTH
    must be open.

    * `import_autonomy` "review" is exactly "do not decide an import for me":
      the pipeline stops at the first family it cannot fill and hands the album
      over, so a download queued while it is on is left where it is with its
      row, waiting for the press that IS the review.
    * `manual_import_enabled` off means an import the user asked for by hand
      must not run at all (see the key's own contract: those routes refuse with
      `MANUAL_OFF_NOTE`), and an automatic import behind that refusal is the
      very thing the switch exists to stop — "nothing importing behind their
      back".

    Neither switch is about ACQUISITION, which is why `auto_acquisition_enabled`
    is not asked here: the download itself was the user's own action (the same
    reason a wish's "Search now" ignores that switch), and this call only
    decides what happens to the bytes they asked for.
    """
    return manual_import_enabled(cfg) and mode(cfg) == "automatic"


def configured_families(cfg):
    """The family ids `import_review_families` names, unknown names dropped."""
    raw = (cfg or {}).get("import_review_families")
    if isinstance(raw, str):
        raw = raw.replace("\n", ";").replace(",", ";").split(";")
    if not isinstance(raw, (list, tuple)):
        return ()
    out = []
    for item in raw:
        fid = str(item or "").strip().lower()
        if fid in _BY_ID and fid not in out:
            out.append(fid)
    return tuple(sorted(out, key=order_of))


def review_families(cfg):
    """The families the USER decides, not the pipeline.

    Review mode keeps every family: that is what the mode means — the wizard's
    stop-at-each-step behaviour applied to the pipeline. Automatic mode keeps
    the configured list plus whichever families are already under review by
    their own switch (`cover_review`), so a family whose pick the user asked
    for stays theirs and the report says so.
    """
    if mode(cfg) == "review":
        return FAMILY_IDS
    out = list(configured_families(cfg))
    for entry in FAMILIES:
        key = entry.get("review_key")
        if key and (cfg or {}).get(key, False) and entry["id"] not in out:
            out.append(entry["id"])
    return tuple(sorted(out, key=order_of))


def forced_families(cfg):
    """The families whose steps this import must NOT decide.

    Only a family a person actually asked for changes the config it runs
    with: an entry of `import_review_families`, or — in review mode, where
    every step is the user's — all of them. A family that is under review
    merely because its own switch says so (`cover_review` on) needs no
    override: that switch already keeps it.
    """
    if mode(cfg) == "review":
        return FAMILY_IDS
    return configured_families(cfg)


def effective_config(cfg):
    """`cfg` with every family the user kept for themselves left undecided.

    Nothing else changes, and nothing is overruled: a family NOT in the list
    keeps whatever its own switches say, so the pipeline's automatic behaviour
    stays the settings' to decide.
    """
    out = dict(cfg or {})
    for fid in forced_families(out):
        out.update(_BY_ID[fid].get("off") or {})
    return out


def dropped_chain_ids(cfg):
    """Script ids the chain must not run because a family is under review.

    Script 13 fetches lyrics whatever any switch says, so keeping lyrics for
    the user means dropping the fetch — and with it script 17, which reads the
    lyrics 13 would have written and decides the transliteration/translation
    half of the same family. `server.imports.chain_for` is the one place that
    computes a chain, so the preview and the run agree.
    """
    out = set()
    for fid in forced_families(cfg):
        out.update(_BY_ID[fid].get("chain") or ())
    return out


def plan(cfg, album_dir=None):
    """This import's policy: ``{"mode", "review", "forced", "stop"}``.

    *stop* is the family the pipeline hands the album over at: in review mode
    the first family the album is missing (the earliest wizard step that needs
    a decision), and None in automatic mode, which never stops. Deciding it
    needs the album's state, so it is read before the chain touches anything —
    a review is about what ARRIVED, not about what the chain could add.
    """
    cfg = cfg or {}
    stopped = None
    if mode(cfg) == "review" and album_dir:
        for fid in FAMILY_IDS:
            if fid in gaps(album_dir, cfg):
                stopped = fid
                break
    return {"mode": mode(cfg), "review": review_families(cfg),
            "forced": forced_families(cfg), "stop": stopped}


def _codes_by_family():
    """family id -> {grader issue code: the field it names}."""
    return {f["id"]: dict(f["codes"]) for f in FAMILIES}


def gaps(album_dir, cfg, steps=None, grade=None):
    """What this album is still missing, per family.

    The grader is the source: `mlo.grader._grade_album` is run exactly as the
    Grade script runs it (same config, same lyrics format), and its per-track
    issue codes are folded into the family that owns them. Nothing here forms
    an opinion of its own about what "complete" means.

    *grade* is a grade the CALLER already has for this album — `server.imports
    .finish_album` hands over what the chain's own Grade step (script 4, LAST
    in the shipped `run_all_order`) produced seconds earlier — and it is used
    INSTEAD of grading again when it answers the same question, i.e. when the
    config it was taken with is the `grade_cfg` built below. That is one grade
    per import instead of two identical ones. It is not a memo: nothing is
    cached, and a caller with nothing in hand pays for its own grade exactly as
    before. The caller decides whether its grade is an answer at all — see the
    `_grade_sink` note in `finish_album` for the config under which it is not.

    The two exceptions are families the grader cannot report: an absent
    ITUNESADVISORY is a legitimate "unrated" and a staged cover is a file the
    grader is right not to count. *steps* is the pipeline's own results
    (``{"advisory": …, "cover": …}``, straight out of the steps it ran), and it
    is what says those two were left open rather than never asked about.

    Returns ``{family_id: {"id", "label", "step", "state", "fields", "codes",
    "note"}}``, ordered like the wizard's steps. *state* is "decision" when the
    pipeline left the family to the user (or was told not to decide it) and
    "unsourced" when the sources were asked and none could supply it.
    """

    cfg = cfg or {}
    steps = steps or {}
    out = {}
    review = set(review_families(cfg))
    # A family the user kept for THEMSELVES is still a gap this report has to
    # name: `mlo.config.tag_write_enabled` stops requiring a tag whose writer is
    # switched off, so grading with the held family's own switch off hid exactly
    # the decision the review exists to hand over — the Genres family held by
    # hand produced no GENRE_MISSING and therefore no prompt at all. Only the
    # GRADE is asked with those switches back on; no step runs, and the entry's
    # `state` is `decision` either way (see `_entry`).
    grade_cfg = dict(cfg)
    for fid in review:
        for key, off_value in (_BY_ID[fid].get("off") or {}).items():
            if isinstance(off_value, bool):
                grade_cfg[key] = not off_value
            else:
                grade_cfg[key] = off_value

    try:
        if grade is not None:
            # One grade per import (spec R155): this is the grade the chain's
            # own Grade step just produced for this album — the same checks on
            # the same folder with the same switches, because the caller only
            # offers it when the configs match (see `finish_album`). Grading
            # again here produced the identical answer a second time, which is
            # the whole cost this removes.
            res = grade
        else:
            from .grader import _grade_album
            res = _grade_album(
                album_dir, str(grade_cfg.get("lyrics_format", "EMBEDDED")).upper(),
                grade_cfg)
    except Exception:
        res = None
    codes = set()
    for track in (res or {}).get("tracks") or []:
        codes.update(str(c) for c in (track.get("issues") or []))
    for fid, names in _codes_by_family().items():
        hits = sorted(codes & set(names))
        if not hits:
            continue
        entry = _entry(fid, review, names, hits)
        step = steps.get(fid)
        if isinstance(step, dict):
            # The step that ran knows more than the grade does: a cover step
            # that staged candidates has an answer waiting, one that found
            # nothing does not.
            entry["state"] = "decision" if step.get("staged") else "unsourced"
            entry["note"] = str(step.get("note") or "")
        out[fid] = entry

    advisory = steps.get("advisory")
    if "advisory" not in out:
        note = _advisory_note((res or {}).get("tracks") or [], advisory)
        if note:
            # The second family the grade cannot report (see the docstring):
            # nothing stated a value and the ladder was told to invent none —
            # or the fetch never ran because the step is the user's. Values
            # already on the files are a rating and never a gap.
            out["advisory"] = _entry(
                "advisory", review, _BY_ID["advisory"]["codes"], [],
                state="decision" if (advisory is None or advisory.get("skipped")) else None,
                note=note)

    return {fid: out[fid] for fid in FAMILY_IDS if fid in out}


def _advisory_note(tracks, step):
    """Why the advisory is still open, or "" when it is not.

    A track that already carries a 0/1/2 is rated: the value the grade read is
    the user's own (or a provider's from an earlier run) and is not a gap.
    """
    for track in tracks:
        if str((track.get("values") or {}).get("ITUNESADVISORY") or "").strip():
            return ""
    if step is None or step.get("skipped"):
        return "not fetched — this step is yours to answer"
    if not step.get("values"):
        return "no source stated an advisory"
    return ""


def _entry(family_id, review, names, hits, state=None, note=""):
    entry = _BY_ID[family_id]
    return {
        "id": family_id,
        "label": entry["label"],
        "step": entry["step"],
        "state": state or ("decision" if family_id in review else "unsourced"),
        "fields": [names[c] for c in hits] if hits else [entry["label"].lower()],
        "codes": list(hits),
        "note": note,
    }


def wizard_link(album_dir, families):
    """The wizard URL for these families: the album, the step the user should
    land on (the first one that needs a decision) and the whole list, so the
    page can keep saying what is still missing on every other step."""
    from urllib.parse import quote

    ids = [fid for fid in sorted(set(families), key=order_of)]
    first = ids[0] if ids else None
    step = _BY_ID[first]["step"] if first else ""
    url = "/import?album=" + quote(str(album_dir or ""), safe="")
    if step:
        url += "&step=" + quote(step, safe="")
    if ids:
        url += "&missing=" + ",".join(ids)
    return url
