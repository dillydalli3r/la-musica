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
wizard and the ``POST /api/import/*`` routes). Both default on.
"""

MODES = ("automatic", "review")

# Wizard order — the same order as the wizard's own steps (the Match step
# between Links and Covers is not a family: it decides which RELEASE the album
# is, and a release the import could not identify is reported by the family
# steps that need its identity). `off` is what "do not decide this for me"
# means to the step that reads it, `review_key` the switch a family may
# already be under review by, and `chain` the script ids that would otherwise
# decide it.
FAMILIES = (
    {
        "id": "links",
        "label": "Links",
        "step": "Links",
        "off": {"rym_links_auto": False},
        "codes": {"MB_LINK": "MusicBrainz release link",
                  "RYM_LINK": "RateYourMusic release link"},
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
    },
    {
        "id": "lyrics",
        "label": "Lyrics",
        "step": "Lyrics",
        # No switch makes script 13 write lyrics, so a lyrics review drops the
        # fetch from the chain instead (see `dropped_chain_ids`).
        "chain": (13,),
        "codes": {"LYRICS": "lyrics",
                  "XLIT_MISSING": "lyric transliteration/translation"},
    },
    {
        "id": "advisory",
        "label": "Advisory",
        "step": "Advisory",
        # "none" is the fallback that writes nothing at all: an advisory the
        # ladder was told not to invent is one the user answers.
        "off": {"advisory_auto_fetch": False, "advisory_fallback": "none"},
        "codes": {"ITUNESADVISORY": "advisory"},
    },
)

FAMILY_IDS = tuple(f["id"] for f in FAMILIES)

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
    the user means dropping the fetch — `server.imports.chain_for` is the one
    place that computes a chain, so the preview and the run agree.
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


def gaps(album_dir, cfg, steps=None):
    """What this album is still missing, per family.

    The grader is the source: `mlo.grader._grade_album` is run exactly as the
    Grade script runs it (same config, same lyrics format), and its per-track
    issue codes are folded into the family that owns them. Nothing here forms
    an opinion of its own about what "complete" means.

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

    try:
        from .grader import _grade_album
        res = _grade_album(album_dir, str(cfg.get("lyrics_format", "EMBEDDED")).upper(), cfg)
    except Exception:
        res = None
    codes = set()
    for track in (res or {}).get("tracks") or []:
        codes.update(str(c) for c in (track.get("issues") or []))
    review = set(review_families(cfg))
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
