"""The ONE release-choice policy: which MusicBrainz edition an album IS.

Every acquisition path — "Add to library", the bulk auto-import, the wish
worker and the artist watch — asks this module which edition of a release
group to download, and the Settings page's release-choice keys are the whole of
what it reads. It is a pure ranking over MusicBrainz payloads plus a config
dict: no network, no config file, no state, so the same payloads and the same
config always produce the same pick.

The rules, in the order they decide. Each tier is weighted so heavily that no
lower tier can ever outvote a higher one, which is why the score IS the order:

1. status      official > an unstated status > withdrawn/expired/cancelled >
               promotion > bootleg. An unofficial edition is only ever chosen
               when the group offers nothing official, and its reason says so.
2. medium      `auto_import_medium_order`, best first — CD, then the other
               physical media, digital last by default. A format the order
               does not name ranks after every configured one.
3. set         a box set — media this library cannot use (a DVD, a Blu-ray),
               or three discs of the album — sorts below the album's own.
4. compressed  a release that names itself a re-encode of a disc (BDRip,
               DVDRip, x264, …) sorts below the disc's own streams — a remux or
               a full-disc edition is taken as it comes, never a derivative.
5. tracks      a release short of the release group's OWN track count is
               penalised, so a 1-track promo can never beat the full album.
6. date        the EARLIEST release date wins — the original pressing, not a
               reissue or a deluxe — unless a later one is materially more
               complete (tier 5 outranks this one). The reference is the
               earliest edition this group OFFERS, not the group's stated
               first-release-date: a pressing that predates that date is still
               the earlier record of the two, and an album whose original is
               not on offer is decided by the editions that are.
7. precision   an edition that states its date in full (YYYY-MM-DD) beats one
               that states only its month or its year when the two could be the
               same day — the album folder is named after this date.
8. edition     `prefer_original_edition` (default true): a clean/explicit-edited
               edition sorts below the original — a clean edition may carry
               altered audio.
9. plain       a plain title beats a disambiguated/parenthesised one. A
               MusicBrainz comment ("(BMG Club edition)", "(CB 811)", "edited
               version") is the data saying this edition needed distinguishing,
               so it loses the tie to a title that carries none. Nothing is
               read INTO the comment: club, promo and remaster mean the same
               thing here — one comment against no comment.
10. country    `prefer_release_country` — a TIE-BREAKER and nothing else.

Country is rule 5 in the brief and sorts LAST here on purpose: a tie-breaker
must not outvote any rule above it, and every tier above is lexicographic.

The config's eligibility rules — `auto_import_avoid_promo` and
`auto_import_require_country` — are always EVALUATED and reported
(`Candidate.eligible`, with the reason at the front of its `reasons`), and
`strict` is what makes a pick SKIP those editions: the unattended paths
(auto-import, the watch) pass it so no automatic download can take a
promotion, a bootleg or a country-less edition, while a user-facing choice
(`/api/mb/release-choice`, an explicit `release_mbid`) ranks the same list and
can report "the best edition is one auto-import would refuse". A caller's own
release-group type filter is not a config rule and excludes an edition from
every pick either way.

MusicBrainz fields read, per rule: `status` (release), `media[].format` and
`medium_formats` (release), `track-count` per medium + the release group's own
count (release / release group), `date` (release) against
`first-release-date` (release group), `disambiguation` (release),
`country` (release), `title` (release), and the release group's
`primary-type` / `secondary-types` for the caller's type filter. The public
type vocabulary is `mlo.naming`'s, so a type this module accepts is exactly a
type the rest of the app can name.
"""
import re
from dataclasses import dataclass, replace
from typing import Mapping, Optional

from mlo.naming import RELEASE_TYPES

# Editions a response may list. A group with more is still ranked in full —
# only the response is capped, so a page never carries hundreds of rows.
CANDIDATE_LIMIT = 20

# The status ladder, best first — MusicBrainz's own Status vocabulary is
# Official / Promotion / Bootleg / Pseudo-Release / Withdrawn / Expired /
# Cancelled, and "" is an edition whose status the data does not state.
STATUS_ORDER = ("official", "promotion", "bootleg")
_STATUS_LEVELS = {
    "official": 1.0,
    # Not stated is not a negative: most releases predate the field being
    # filled, so they sort below official but above a stated bad status.
    "": 0.6,
    "withdrawn": 0.4,
    "expired": 0.4,
    "cancelled": 0.4,
    "canceled": 0.4,
    "promotion": 0.2,
    "pseudo-release": 0.2,
    "pseudo release": 0.2,
    "bootleg": 0.1,
}
# What `auto_import_avoid_promo` refuses outright (the old policy's own set:
# a rip that is not the album must never be auto-imported).
_PROMO_STATUSES = frozenset({"promotion", "bootleg", "pseudo-release", "pseudo release"})

# The score is the tier tuple written as one number, base 8, most significant
# tier first — a positional encoding, so a bigger score IS a better pick and
# no lower tier can ever outvote a higher one (a weighted SUM cannot promise
# that: a small loss in an earlier tier could be paid for by a large gain in a
# later one). The digits are the levels quantized to 0..7, which keeps the
# score monotone in every tier while staying readable (0.8757).
_SCORE_BASE = 8
_TIER_NAMES = ("status", "medium", "set", "compressed", "tracks", "date",
               "precision", "edition", "disambiguation", "country")
# What a tie-break sentence calls each tier (see _deciding_reason).
_TIER_LABELS = {
    "status": "release status",
    "medium": "the medium order",
    "set": "the box-set rule",
    "compressed": "the disc-versus-re-encode rule",
    "tracks": "the track count",
    "date": "the release date",
    "precision": "the date-precision rule",
    "edition": "the clean/edited-edition rule",
    "disambiguation": "the plain-title rule",
    "country": "the preferred country",
}
# …and the substance of the three rules a reader cannot get from the losing
# edition's own reasons: that the winner IS the earliest rather than merely
# first in the list, that its fuller date is what beat the other, and that the
# other edition's comment is what lost it. A reason a user has to infer from a
# difference between two rows is the silent tie-break this exists to avoid.
_TIER_NOTES = {
    "date": "the earliest release date offered wins",
    "precision": "a full date beats one that states only its month or its year",
    "disambiguation": "a title with no MusicBrainz disambiguation comment "
                      "ranks above one with it",
}

# A COMPRESSED derivative of a disc: a re-encode someone else made, not the
# disc's own streams. MusicBrainz states these in a fan-rip release's title or
# its disambiguation comment ("… (BDRip 1080p x264)", "DVDRip", "WEBRip"),
# never in a field of its own — so the marker is the word.
#
# Deliberately NOT in this list: `remux`, `bdmv`, `dvd`, `blu-ray` (the disc
# ITSELF — that is what wins), and codec names like `hevc`/`h264`, which a
# remux can carry just as well. `x264`/`x265`/`xvid`/`divx` ARE here: those
# only ever name an encode someone ran over the source.
_COMPRESSED_RE = re.compile(
    r"\b(?:bdrip|brrip|dvdrip|dvd-rip|webrip|web-dl|hdtv|hdtvrip|"
    r"x264|x265|xvid|divx|microhd|halfcd|half-cd|"
    r"re-?encode[ds]?|compressed)\b",
    re.IGNORECASE,
)


def is_compressed_release(release):
    """Whether *release* names itself a compressed derivative of a disc.

    See `_COMPRESSED_RE`; the fields read are the same two the clean-edition
    rule reads (the title and MusicBrainz's disambiguation comment).
    """
    node = release if isinstance(release, dict) else {}
    text = f"{node.get('title') or ''} {node.get('disambiguation') or ''}"
    return bool(_COMPRESSED_RE.search(text))


# A clean/edited edition: MusicBrainz states this in the release's title or its
# disambiguation comment, never in a field of its own.
_CLEAN_RE = re.compile(r"\bclean\b|\bedited\b|\bcensored\b|\bradio edit\b", re.IGNORECASE)

_RULES = (
    "official beats promotion beats bootleg — an unofficial edition is only "
    "chosen when nothing official exists",
    "the configured medium order decides first: CD, then the other physical "
    "media, digital last",
    "a release short of the release group's own track count is penalised",
    "a box set — an edition carrying DVD/Blu-ray media, or one disc after "
    "another — sorts below the album's own CD/digital media",
    "a COMPRESSED derivative of a disc (a BDRip/DVDRip/x264 re-encode) sorts "
    "below the disc's own streams, which are taken as they are",
    "the EARLIEST release date wins: an earlier edition of the group beats a "
    "later reissue or deluxe unless the later one is materially more complete",
    "an edition that states its release date in full (YYYY-MM-DD) beats one "
    "that states only its month or its year when the two could be the same "
    "day — the album folder is named after it",
    "prefer_release_country only ever breaks a tie",
    "prefer_original_edition prefers the original over a clean/edited edition",
    "a plain title beats a disambiguated one: a comment like \"(BMG Club "
    "edition)\" loses the tie to a title that carries none",
)


# --------------------------------------------------------------------------- #
# MusicBrainz payload access — a browse row (inc=media), a normalized row
# (integrations.release_group_browse) and a full release
# (integrations.release_lookup) all read as one release here: the caller must
# not have to care which of the three it was holding.
# --------------------------------------------------------------------------- #
def release_id(rel):
    """The release's MusicBrainz id, whatever key the payload spells it with."""
    return str(rel.get("id") or rel.get("release_mbid") or "").strip()


_CATALOG_STRIP = re.compile(r"[\s\-_.]+")


def catalog_key(number):
    """One catalog number folded for COMPARISON — case, spaces, dashes, dots.

    What a number IS is its digits and letters; how it is spelled is whoever
    printed it. MusicBrainz records each label's own spelling, and one pressing
    issued by two labels really does appear twice: DGC's ``GED 24425`` beside
    Geffen's ``GED24425`` (both catalogued releases of one CD). The search that
    either one produces finds the same peer folders, so the two are one thing to
    try — `distinct_pressings` is what says so.
    """
    return _CATALOG_STRIP.sub("", str(number or "").strip().upper())


def catalog_numbers(rel):
    """Every catalog number a release states, in MusicBrainz's own order.

    Both payload shapes are read: the browse's per-release ``label-info`` (a
    release can carry several numbers, one per label) and the normalized
    ``catalog_numbers`` / ``catalog_number`` a lookup returns. Blank entries are
    dropped and repeats are folded, because a number printed by two labels is
    still one number.
    """
    out = []
    for entry in (rel.get("label-info") or []):
        num = str((entry or {}).get("catalog-number") or "").strip()
        if num and catalog_key(num) not in {catalog_key(n) for n in out}:
            out.append(num)
    if not out:
        for value in (list(rel.get("catalog_numbers") or [])
                      + [rel.get("catalog_number")]):
            num = str(value or "").strip()
            if num and catalog_key(num) not in {catalog_key(n) for n in out}:
                out.append(num)
    return out


def distinct_pressings(rows):
    """``(kept, skipped)`` — the ranked rows that are DISTINCT SEARCHES.

    The catalog number is what a CD search is keyed on, and separate
    MusicBrainz releases really do share one — the same pressing issued under
    two labels, a reissue catalogued twice, a country variant printed with the
    number unchanged. Asking the network for the second of those can only find
    the SAME folders the first one did, which is a whole search window spent for
    nothing out of a walk that may only spend a few (R151). So an edition
    sharing a folded catalog number with an edition already in the list is
    skipped, and the skipped rows are RETURNED rather than swallowed: the walk
    logs them, so a fallback that dropped a ranked edition says which.

    An edition stating NO catalog number is always kept: there is nothing to
    compare it by, and a pressing with no number may still be a different
    upload (its search is built from the artist, title, date and label instead).
    The first row is always kept, so a walk never comes back empty.
    """
    kept, skipped, seen = [], [], set()
    for row in (rows or []):
        nums = {catalog_key(n) for n in ((row or {}).get("catalog_numbers") or [])
                if catalog_key(n)}
        if nums and nums & seen:
            skipped.append(row)
            continue
        seen |= nums
        kept.append(row)
    return kept, skipped


def media_formats(rel):
    """MusicBrainz format names of a release's media ("CD", "Digital Media").

    `media[].format` first (browse and lookup payloads), then the normalized
    `medium_formats` / `formats` keys, then the single `medium` a row carries
    for its first disc. `release_lookup`'s own `media` is a flat TRACK list
    with no format, so an entry without one is skipped instead of being
    mistaken for a medium.
    """
    media = rel.get("media")
    if isinstance(media, list):
        out = [str(m.get("format") or "").strip()
               for m in media if isinstance(m, dict) and m.get("format")]
        if out:
            return out
    for key in ("medium_formats", "formats"):
        value = rel.get(key)
        if isinstance(value, (list, tuple)):
            out = [str(v).strip() for v in value if str(v).strip()]
            if out:
                return out
        elif isinstance(value, str) and value.strip():
            return _summary_formats(value)
    single = str(rel.get("medium") or "").strip()
    return [single] if single else []


def _summary_formats(text):
    """"2×CD + DVD" → ["CD", "CD", "DVD"] (the browse row's summary string)."""
    out = []
    for part in str(text).split(" + "):
        part = part.strip()
        if not part:
            continue
        if "×" in part:
            count, _, name = part.partition("×")
            try:
                times = max(1, min(20, int(count)))
            except ValueError:
                times = 1
            part = name.strip()
        else:
            times = 1
        out.extend([part] * times)
    return out


def track_count(rel):
    """How many tracks the release carries, 0 when nothing states it.

    Read from the normalized `track_count`, then from each medium's
    `track-count`, then (for a `release_lookup` payload, whose `media` is one
    entry per track) from the number of tracks themselves.
    """
    value = rel.get("track_count")
    try:
        if int(value) > 0:
            return int(value)
    except (TypeError, ValueError):
        pass
    media = rel.get("media")
    if isinstance(media, list) and media:
        counts = [m.get("track-count") for m in media if isinstance(m, dict)]
        total = sum(c for c in counts if isinstance(c, int) and c > 0)
        if total:
            return total
        if all(isinstance(m, dict) and not m.get("format") for m in media):
            return len(media)
    return 0


def group_types(node):
    """(primary_type, secondary_types) of a node's release group.

    A release GROUP payload states them directly; a normalized row carries
    `primary_type`/`secondary_types`; a raw release states them inside its
    embedded `release-group` object. Anything else reads as "type unknown"
    rather than raising.
    """
    primary = str(node.get("primary_type") or node.get("primary-type") or "").strip()
    secondary = [str(s).strip() for s in (node.get("secondary_types")
                                          or node.get("secondary-types") or [])
                 if str(s).strip()]
    if primary or secondary:
        return primary, secondary
    rg = node.get("release-group")
    if isinstance(rg, dict):
        return (str(rg.get("primary-type") or "").strip(),
                [str(s).strip() for s in (rg.get("secondary-types") or []) if str(s).strip()])
    return "", []


def first_release_date(node):
    """The release group's first-release-date, "" when it states none."""
    return str(node.get("first_release_date") or node.get("first-release-date") or "").strip()


def type_matches(primary_type, secondary_types, wanted):
    """Whether a release group's type is one of the *wanted* selections.

    A selection naming ONE type is that type, by the SAME rule the artist
    watch applies to what it may queue (`server.artist_watch.type_matches`): a
    group that states secondary types is matched by those alone — a live album
    is selected as "live", not as "album" — otherwise by its primary type.

    A selection naming SEVERAL parts is a type as MusicBrainz (and the artist
    page) spells a COMBINED one — "Album + Compilation", "album+compilation" —
    and it is matched as the group's WHOLE type: the first part is the primary
    type and the rest are exactly its secondary types (order-insensitive,
    because MusicBrainz files them in its own order). The artist page's "Album
    + Live" row therefore selects the live albums and not its plain albums,
    and its "Album" row selects neither.

    An EMPTY selection matches nothing: "no type selected" is not a licence to
    match everything.
    """
    secondary = [str(s).strip().lower() for s in (secondary_types or ())
                 if str(s).strip()]
    primary = str(primary_type or "").strip().lower()
    for value in (wanted or ()):
        parts = [p.strip().lower() for p in _split_types(value) if p.strip()]
        if not parts:
            continue
        if len(parts) > 1:
            if parts[0] == primary and set(parts[1:]) == set(secondary):
                return True
            continue
        name = parts[0]
        if any(s == name for s in secondary):
            return True
        if not secondary and primary and primary == name:
            return True
    return False


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Candidate:
    """One scored edition, with the facts that put it where it is.

    `score` is 0..1, higher is better, and it encodes the tier order (see the
    module docstring) — comparing two scores compares the two editions' rules
    in order. `reasons` is what a UI shows the user and what the importer logs:
    the facts that decided this candidate's place, in tier order, plus (for the
    pick) the tier it beat the runner-up on.

    `eligible` is false when an unattended download must not take this edition
    (the config's promo/country rules) OR when it is not the release-group type
    the caller asked for; `type_ok` keeps the second apart, because the
    caller's own filter excludes an edition from the CHOICE while the config's
    rules only do so for the unattended paths (see `pick`).
    """
    release_mbid: str
    title: str
    date: str
    country: str
    status: str
    media: tuple
    track_count: int
    disambiguation: str
    # Every catalog number MusicBrainz states for this edition, in its own
    # order. `distinct_pressings` is what reads them: separate releases sharing
    # one number are one SEARCH, so a walk asks only the distinct ones.
    catalog_numbers: tuple = ()
    score: float = 0.0
    reasons: tuple = ()
    eligible: bool = True
    index: int = 0
    type_ok: bool = True

    def to_dict(self):
        return {
            "release_mbid": self.release_mbid,
            "title": self.title,
            "date": self.date,
            "country": self.country,
            "status": self.status,
            "media": list(self.media),
            "track_count": self.track_count,
            "disambiguation": self.disambiguation,
            "catalog_numbers": list(self.catalog_numbers),
            "score": self.score,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class _Context:
    """Everything the rules need that is not the release itself."""
    order: tuple
    country: str
    keep_disc_streams: bool
    keep_original: bool
    avoid_promo: bool
    require_country: bool
    strict: bool
    wanted: tuple
    asked_type: bool
    primary: str
    secondary: tuple
    expected: int
    expected_stated: bool
    first_date: str
    first_year: Optional[int]
    # The earliest edition this payload OFFERS — the date tier's reference
    # (see `_release_date_level`), and the three facts a reason needs to name
    # it. None/"" when nothing states a date.
    earliest_date: str
    earliest_key: Optional[int]
    earliest_year: Optional[int]
    has_official: bool


def policy_config(cfg=None):
    """The config the policy reads, with the shipped defaults filled in.

    A partial dict (a test, the CLI) gets the defaults for everything it does
    not state, so the default medium order lives exactly once — in
    `mlo.config.DEFAULT_CONFIG`.
    """
    from mlo.config import DEFAULT_CONFIG

    merged = dict(DEFAULT_CONFIG)
    if isinstance(cfg, Mapping):
        merged.update({k: v for k, v in cfg.items() if v is not None})
    order = [str(x).strip() for x in (merged.get("auto_import_medium_order") or [])
             if str(x).strip()]
    merged["auto_import_medium_order"] = (
        order[:12] or list(DEFAULT_CONFIG["auto_import_medium_order"]))
    return merged


def policy_report(cfg=None):
    """The policy as data — the Settings keys, the ladder and the rules."""
    conf = policy_config(cfg)
    return {
        "medium_order": list(conf["auto_import_medium_order"]),
        "preferred_country": str(conf.get("prefer_release_country") or ""),
        "prefer_original_edition": bool(conf.get("prefer_original_edition", True)),
        "prefer_disc_streams": bool(conf.get("prefer_disc_streams", True)),
        "status_order": list(STATUS_ORDER),
        "rules": list(_RULES),
    }


def expected_tracks(release_group, releases):
    """The release group's own track count.

    MusicBrainz states no count on a release group, so the fullest edition
    offered is what "the release group's track count" means in practice — and
    when the caller does state one, that is what a release is measured
    against. 0 when nothing states a count at all.
    """
    stated = _stated_track_count(release_group)
    if stated:
        return stated
    counts = [track_count(r) for r in releases or []]
    return max([c for c in counts if c > 0] or [0])


def _stated_track_count(node):
    """The track count a release-group payload states itself, 0 when none."""
    try:
        value = int((node or {}).get("track_count"))
    except (TypeError, ValueError, AttributeError):
        return 0
    return value if value > 0 else 0


def _wanted_types(wanted_types, primary_type, secondary_type):
    """(recognized type names, whether the caller asked for any).

    Names come from `mlo.naming`'s public vocabulary (MusicBrainz's own
    names), so a caller can pass the same strings the watch's type filter and
    the artist page's chips use. An unrecognised name is dropped — and
    `asked` tells the caller's mistake apart from "no filter".
    """
    names = []
    for value in (wanted_types or ()):
        names.extend(_split_types(value))
    for value in (primary_type, secondary_type):
        names.extend(_split_types(value))
    out = []
    for name in names:
        n = str(name or "").strip().lower()
        if n in RELEASE_TYPES and n not in out:
            out.append(n)
    return tuple(out), bool(names)


def _split_types(value):
    return [p for p in re.split(r"\s*[+;,]\s*", str(value or "")) if p.strip()]


def type_names(values):
    """*values* as MusicBrainz's own lowercase release-group type SELECTIONS.

    THE one place a caller-supplied type selection is normalized. Every value
    the caller passed stays ONE selection: a combined spelling ("Album +
    Compilation", "album+compilation", "Album; Compilation") is normalized in
    place ("album + compilation") rather than split into the names it is made
    of, because that is what the caller meant and what `type_matches` reads —
    a selection naming several parts is the group's WHOLE type (an album that
    IS a compilation), while the parts on their own read as "album OR
    compilation" and queue the plain albums and the compilations of other
    types nobody asked for. Every part is still validated against
    `mlo.naming`'s published vocabulary, so a name outside it raises
    ValueError — a filter that could never match anything is a user error to
    REPORT, never a silent no-op that quietly downloads nothing — and the
    selections are de-duplicated in the caller's order.
    `server.artist_watch.clean_types` stores a watch's own selection through
    this, so a watch and an add read one vocabulary, one selection at a time.
    """
    out = []
    for value in (values or ()):
        parts = []
        for raw in _split_types(value):
            part = str(raw).strip().lower()
            if not part:
                continue
            if part not in RELEASE_TYPES:
                raise ValueError(f"unknown release-group type {raw!r}")
            parts.append(part)
        selection = " + ".join(parts)
        if selection and selection not in out:
            out.append(selection)
    return out


# The media that make an edition a BOX SET rather than the album. MusicBrainz
# states these as medium formats, and they are the ones an audio library cannot
# use: a DVD rip is a video file, a Blu-ray is 25 GB of the same, and both
# arrive bundled with the album's CD in the deluxe/anniversary boxes that
# otherwise answer to the same names as a plain CD.
#
# `DVD Audio` is deliberately NOT here: it is an audio medium (and the audio
# exists nowhere else). The comparison is exact after folding case, hyphens and
# spaces, plus the unambiguous substrings — so "Blu-ray" catches MusicBrainz's
# "Blu-ray", "Blu-ray-R" and "BD-R", and "dvd" alone would have caught the
# audio format this rule must not touch.
_VIDEO_FORMATS = frozenset({
    "dvd", "dvd-video", "dvdvideo", "bluray", "blurayr", "bdr", "hddvd",
    "hddvd", "vhs", "vcd", "svcd", "videocd", "laserdisc", "umd", "betamax",
    "ced", "video8", "hi8",
})
_VIDEO_SUBSTRINGS = ("blu-ray", "hd dvd", "hi8", "laserdisc", "vhs", "svcd", "vcd")


def _norm_format(name):
    """A medium format folded for comparison: case, hyphens and runs of space."""
    return re.sub(r"[\s\-_]+", "", str(name or "").strip().lower())


def is_video_format(name):
    """Whether one MusicBrainz medium format names VIDEO media."""
    folded = _norm_format(name)
    if not folded:
        return False
    if folded in _VIDEO_FORMATS:
        return True
    # The substrings are matched on the folded spelling too ("blu-ray" folds to
    # "bluray" above), so every form of the name is caught by the same test.
    return any(sub.replace("-", "").replace(" ", "") in folded
               for sub in _VIDEO_SUBSTRINGS)


def video_formats(rel):
    """The release's video media, in MusicBrainz's own spelling."""
    return [f for f in media_formats(rel) if is_video_format(f)]


def disc_count(rel):
    """How many media the release holds (1 when MusicBrainz states none)."""
    media = rel.get("media")
    if isinstance(media, list) and media:
        return len([m for m in media if isinstance(m, dict)])
    return max(1, len(media_formats(rel)))


def medium_rank(rel, order):
    """(index, label) of the release's best medium in *order*.

    `index` is 0 for the most preferred medium and len(order) for a format the
    order does not name (which ranks after every configured one, so an unknown
    label keeps its configured neighbours' positions instead of leading).
    `label` is the medium that matched, or the first one that did not.
    """
    names = media_formats(rel)
    best, label = len(order), (names[0] if names else "")
    for name in names:
        n = name.lower()
        for i, pref in enumerate(order):
            # Labels compare case-insensitively, and a configured label matches
            # the formats that CONTAIN it — "CD" must catch MusicBrainz's
            # "8cm CD"/"HDCD", "Vinyl" its 7"/12" pressings.
            p = pref.lower()
            if p and (n == p or p in n):
                if i < best:
                    best, label = i, name
                break
    return best, label


def _stated_granularity(date):
    """What MusicBrainz actually STATES: "day", "month", "year" or "".

    The release date field is free text that may carry any of the three
    (a browse payload famously has "1973" for a pressing and "1973-03-01"
    for the next one), and the difference is the difference between a folder
    named after a year and one named after the day.
    """
    return {10: "day", 7: "month", 4: "year"}.get(
        len(str(date or "").strip()), "")


def _date_precision(date):
    """How much of the date MusicBrainz states: 1.0 day, 0.66 month, 0.33
    year, 0.0 nothing — the date tier's own tie-break (rule 7).

    The album folder is named after this date, so an edition stating only
    "1983" pins the folder to a year while one stating "1983-09-13" pins it to
    the day — and when two editions could be the same day, the one that states
    more of its date is the one to take."""
    return {"day": 1.0, "month": 0.66, "year": 0.33}.get(
        _stated_granularity(date), 0.0)


def _year(date):
    text = str(date or "").strip()
    try:
        return int(text[:4])
    except (TypeError, ValueError):
        return None


# One year in `_date_key` units (see it): a year is 12 months of 31, so no two
# years can overlap and the date tier needs no calendar.
_YEAR_KEY = 372


def _date_key(date):
    """The date as one sortable number — the END of the range it states, None
    when nothing usable is stated.

    MusicBrainz states a date at whatever precision it knows, and an edition
    that states only "1983" is not a pressing from New Year's Day: it is one
    from somewhere in 1983. Reading a date as the LAST day its range can mean
    is what lets ONE comparison answer both halves of the date order: an
    earlier edition is the earlier range (a bare year ends in December, so
    every dated edition inside that year ends before it), and when two ranges
    end on the same day the precision tier says which of them is the more
    precise fact — "1983-12-31" against "1983".

    An unstated or unreadable part is the end of what WAS stated, so a
    malformed date degrades to the coarser reading rather than raising: this
    runs over MusicBrainz's free text.
    """
    year = _year(date)
    if year is None:
        return None
    text = str(date).strip()
    stated = _stated_granularity(date)
    month = 0
    if stated in ("month", "day"):
        try:
            month = int(text[5:7])
        except (TypeError, ValueError):
            month = 0
        if not 1 <= month <= 12:
            month = 0
    day = 0
    if stated == "day":
        try:
            day = int(text[8:10])
        except (TypeError, ValueError):
            day = 0
        if not 1 <= day <= 31:
            day = 0
    return (year * _YEAR_KEY + (month - 1 if month else 11) * 31
            + (day - 1 if day else 30))


def _release_date_level(date, ctx):
    """(level, reason) for the date tier — the EARLIEST release date wins.

    The reference is the earliest edition this release group OFFERS
    (`ctx.earliest_key`), and an earlier one of those wins outright. Not the
    group's stated first-release-date: a pressing that predates it is still
    the earlier record of the two, and a group whose original is not on offer
    (a 1973 first release beside two 2010s remasters) is decided by the
    editions that are, rather than by a date none of them states. The penalty
    for being later is strictly decreasing in the distance and never flat, so
    two reissues a decade apart are never a tie and listing order can never
    promote the later one — that is how a 2016 remaster once came back as "The
    Dark Side of the Moon" and named the album folder 2016. An undated edition
    sorts after every dated one.
    """
    key = _date_key(date)
    if key is None:
        return (0.0, "no release date on MusicBrainz")
    gap = max(0, key - ctx.earliest_key) if ctx.earliest_key is not None else 0
    # Hyperbolic, NOT a line that can reach zero: a linear term hit 0 at a
    # nine-year gap, so every edition more than nine years after the earliest
    # scored the same and a 2016 CD could beat a 2011 one on nothing but
    # MusicBrainz's listing order.
    level = 1.0 / (1.0 + gap / _YEAR_KEY)
    # Say WHICH part is missing: "1983-06" is not "only the year", and a
    # reason that names the wrong field reads as a bug in the data.
    stated = _stated_granularity(date)
    vague = (f" (MusicBrainz states only the {stated})"
             if stated in ("month", "year") else "")
    if not gap:
        # The earliest edition offered from the group's own first-release
        # year IS the original; anything else is the earliest this group
        # happens to offer, and the reason says which of the two it is.
        if ctx.first_date and (_year(date) or 0) <= (ctx.first_year or 0):
            return (level, f"original release date {ctx.first_date}" + vague)
        return (level, f"earliest edition offered, {date}" + vague)
    years = max(0, (_year(date) or 0) - (ctx.earliest_year or 0))
    later = (f"{years} year(s) after" if years
             else "later the same year as")
    return (level, f"reissued {date} — {later} the earliest edition offered "
                   f"({ctx.earliest_date}){vague}")


def _status_reason(status):
    ladder = {
        "official": "official release",
        "promotion": "promotion — not a released edition",
        "bootleg": "bootleg — not an official edition",
        "pseudo-release": "pseudo-release — not an official edition",
        "pseudo release": "pseudo-release — not an official edition",
        "withdrawn": "withdrawn by the label",
        "expired": "expired — the label pulled it",
        "cancelled": "cancelled by the label",
        "canceled": "cancelled by the label",
    }
    return ladder.get(status, "MusicBrainz states no release status")


def _set_level(rel):
    """(level, reason) for the box-set tier — rule 3 in the module docstring.

    A box set is not a bigger album: it is the album plus media this library
    cannot use (a DVD, a Blu-ray) and, at its worst, four more discs of the
    same record. Both signals score below an edition that holds just the
    album, which is what makes a plain CD or digital release win before the
    track-count and date rules ever see the box.
    """
    video = video_formats(rel)
    if video:
        return 0.0, ("carries " + ", ".join(sorted(set(video)))
                     + " — a box set, not the album's own media")
    discs = disc_count(rel)
    if discs >= 3:
        return 0.35, f"{discs} discs — a box set rather than the album"
    if discs == 2:
        return 0.8, "2 discs"
    return 1.0, "one disc"


def _evaluate(rel, ctx, index):
    """(levels, candidate) for one release — the whole policy, in one pass."""
    status = str(rel.get("status") or "").strip()
    low = status.lower()
    title = str(rel.get("title") or "").strip()
    date = str(rel.get("date") or "").strip()
    country = str(rel.get("country") or "").strip()
    disambiguation = str(rel.get("disambiguation") or "").strip()
    formats = tuple(media_formats(rel))
    count = track_count(rel)
    reasons = []

    # 1. status ...
    level_status = _STATUS_LEVELS.get(low, _STATUS_LEVELS[""])
    reasons.append(_status_reason(low))
    if low != "official" and not ctx.has_official:
        # Rule 1's other half: this is only ever the pick because there is
        # nothing official to take, and the reason has to say so.
        reasons.append("no official edition exists — this is the only kind on offer")

    # 2. medium ...
    rank, label = medium_rank(rel, ctx.order)
    level_medium = ((len(ctx.order) - rank) / len(ctx.order)) if rank < len(ctx.order) else 0.0
    reasons.append(f"{label} — preferred medium (order {rank + 1})" if rank < len(ctx.order)
                   else f"{label or 'no medium stated'} — not in the configured medium order")

    # 3. the set: what the edition actually HOLDS ...
    level_set, set_reason = _set_level(rel)
    reasons.append(set_reason)

    # 4. the disc's own streams, not someone's re-encode of them: a BDRip or a
    #    DVDRip is a lossy derivative, and when the group also offers the disc
    #    (a remux, a full disc, the original pressing) that is what to take.
    if ctx.keep_disc_streams and is_compressed_release(rel):
        level_compressed = 0.0
        reasons.append("a compressed re-release — the disc's own streams are "
                       "preferred over a derivative (prefer_disc_streams)")
    else:
        level_compressed = 1.0

    # 5. completeness ...
    if count <= 0:
        level_tracks = 0.5
        reasons.append("no track count on MusicBrainz — not counted against it")
    elif ctx.expected <= 0:
        level_tracks = 1.0
        reasons.append(f"{count} tracks")
    else:
        level_tracks = min(1.0, count / ctx.expected)
        if count > ctx.expected and ctx.expected_stated:
            # The release group itself says how many tracks the album has, and
            # this edition holds more: that is the bonus-disc half of a box set,
            # scored below the album proper. Only a STATED count is trusted here
            # — when the group states none, `expected` is the fullest edition
            # offered (see `expected_tracks`), and penalising everything below
            # the box would be the opposite of the rule.
            level_tracks = max(0.1, ctx.expected / count)
            reasons.append(f"{count} tracks — {ctx.expected} is the release "
                           "group's own count, so this edition holds more than "
                           "the album")
        elif count >= ctx.expected:
            reasons.append(f"{count}/{ctx.expected} tracks of the release group")
        else:
            reasons.append(f"{count} of {ctx.expected} tracks — short of "
                           + ("the release group's own count" if ctx.expected_stated
                              else "the fullest edition offered"))

    # 6. date — the EARLIEST edition offered wins (see `_release_date_level`).
    level_date, date_reason = _release_date_level(date, ctx)
    reasons.append(date_reason)

    # 7. date precision — the tie-break INSIDE the date rule: two editions that
    #    could be the same day are separated by which of them states more of
    #    its date, because the album folder is named after it.
    level_precision = _date_precision(date)

    # 8. edition kind ...
    clean = bool(_CLEAN_RE.search(f"{title} {disambiguation}"))
    if clean and ctx.keep_original:
        level_edition = 0.0
        reasons.append("clean/edited edition — prefer_original_edition prefers the original")
    else:
        level_edition = 1.0
        if clean:
            reasons.append("clean edition — prefer_original_edition is off, so it is not penalised")

    # 9. plain title — a MusicBrainz disambiguation comment is that data saying
    #    this edition needed distinguishing, so a title carrying one loses the
    #    tie to a title that carries none. Nothing is read INTO the comment
    #    (see the module docstring): club, promo and remaster are one case.
    level_plain = 0.0 if disambiguation else 1.0
    if disambiguation:
        reasons.append(f'MusicBrainz disambiguation "{disambiguation}" — a '
                       "title with no comment ranks above it")

    # 10. country (a tie-breaker, so it is the last tier) ...
    if ctx.country:
        if country and country.lower() == ctx.country.lower():
            level_country = 1.0
            reasons.append(f"{country} — preferred release country")
        else:
            level_country = 0.0
            if country:
                reasons.append(f"{country} — not the preferred release country ({ctx.country})")
    else:
        level_country = 1.0

    # The caller's own type filter, then the config's eligibility verdict.
    # `eligible` is what an UNATTENDED download would do with this edition
    # (reported whether or not the caller asked the pick to skip it), so a UI
    # can show "the policy's best edition, and auto-import will refuse it" —
    # while `strict` is what makes `pick` actually skip it.
    type_ok = True
    if ctx.asked_type and not type_matches(*_row_types(rel, ctx), ctx.wanted):
        type_ok = False
        reasons.insert(0, "release group type is not the type asked for")
    eligible = type_ok
    if ctx.avoid_promo and low in _PROMO_STATUSES:
        eligible = False
        reasons.insert(0, "auto-import never takes a promotional or bootleg "
                          "edition (auto_import_avoid_promo)")
    if ctx.require_country and not country:
        eligible = False
        reasons.insert(0, "no release country on MusicBrainz — auto-import "
                          "skips it (auto_import_require_country)")

    candidate = Candidate(
        release_mbid=release_id(rel), title=title, date=date, country=country,
        status=status, media=formats, track_count=count,
        disambiguation=disambiguation, catalog_numbers=tuple(catalog_numbers(rel)),
        reasons=tuple(reasons), eligible=eligible,
        index=index, type_ok=type_ok,
    )
    return ((level_status, level_medium, level_set, level_compressed, level_tracks,
             level_date, level_precision, level_edition, level_plain,
             level_country), candidate)


def _row_types(rel, ctx):
    """(primary, secondary) for the type filter: the row's own group types when
    it states them, the release group's otherwise."""
    primary, secondary = group_types(rel)
    if primary or secondary:
        return primary, secondary
    return ctx.primary, ctx.secondary


def rank_releases(release_group, releases, cfg=None, *, strict=False,
                  primary_type="", secondary_type="", wanted_types=None):
    """Every edition of *release_group*, scored and ranked best first.

    `release_group` is the group's payload (`integrations.release_group_browse`:
    title, first_release_date, primary_type, secondary_types) or an empty dict
    when the caller only holds editions. `strict` applies the config's
    eligibility rules (see the module docstring); `primary_type`/`secondary_type`
    (or `wanted_types`) restrict the pick to the release-group type the caller
    is after, using `mlo.naming`'s vocabulary.

    Deterministic: the sort is by the tier tuple, then by the order the
    releases arrived in, so identical inputs always rank identically.
    """
    conf = policy_config(cfg)
    rows = list(releases or [])
    wanted, asked_type = _wanted_types(wanted_types, primary_type, secondary_type)
    primary, secondary = group_types(release_group) if isinstance(release_group, Mapping) else ("", [])
    # The group's own first-release-date is read for WORDING alone (the earliest
    # edition offered IS the original when the two agree); what the date tier
    # ranks against is the earliest edition in *rows*, so a group whose stated
    # date the provider never offered — or a pressing that predates it — is
    # still decided by the records in hand.
    first = first_release_date(release_group) if isinstance(release_group, Mapping) else ""
    first_year = _year(first) if first else None
    earliest_date, earliest_key, earliest_year = "", None, None
    for r in rows:
        key = _date_key(r.get("date"))
        if key is None:
            continue
        if earliest_key is None or key < earliest_key:
            earliest_date = str(r.get("date") or "").strip()
            earliest_key, earliest_year = key, _year(r.get("date"))
    ctx = _Context(
        order=tuple(conf["auto_import_medium_order"]),
        country=str(conf.get("prefer_release_country") or "").strip(),
        keep_disc_streams=bool(conf.get("prefer_disc_streams", True)),
        keep_original=bool(conf.get("prefer_original_edition", True)),
        avoid_promo=bool(conf.get("auto_import_avoid_promo", True)),
        require_country=bool(conf.get("auto_import_require_country", True)),
        strict=bool(strict),
        wanted=wanted,
        asked_type=asked_type,
        primary=primary,
        secondary=tuple(secondary),
        expected=expected_tracks(release_group if isinstance(release_group, Mapping) else {}, rows),
        expected_stated=bool(_stated_track_count(release_group)),
        first_date=first,
        first_year=first_year,
        earliest_date=earliest_date,
        earliest_key=earliest_key,
        earliest_year=earliest_year,
        has_official=any(str(r.get("status") or "").strip().lower() == "official" for r in rows),
    )

    scored = [_evaluate(rel, ctx, i) for i, rel in enumerate(rows)]
    scored.sort(key=lambda pair: (tuple(-v for v in pair[0]), pair[1].index))

    out = []
    for position, (levels, cand) in enumerate(scored):
        reasons = list(cand.reasons)
        if position == 0 and len(scored) > 1:
            reasons.append(_deciding_reason(levels, scored[1][0], scored[1][1]))
        elif position == 0 and len(scored) == 1:
            reasons.append("the only edition of this release group")
        out.append(replace(cand, score=_score(levels), reasons=tuple(reasons)))
    return out


def rank_stored(rows, cfg=None):
    """An ALREADY-STORED candidate list, ranked by THIS policy.

    A wish records the editions behind the one it was added for, and that
    stored list is a SNAPSHOT: it holds what each edition stated when the add
    resolved it and nothing about the policy that ordered it. The order is
    therefore recomputed here — at every read, from those facts alone, with no
    network request and no release-group lookup — so a release queued before a
    rule changed is walked by the rule in force now rather than by the order
    captured at add time.

    Each row is read exactly as a release payload is (`rank_releases` ranks
    them), so a row stating only its id and title ties on every rule and keeps
    the position it was stored in. The sort is stable and nothing is dropped:
    a fallback that silently loses a ranked edition is what the walk exists to
    avoid. A row stating SOME facts ranks on those and ties with its peers on
    the rest — degrading to the rules the data can answer is the honest
    answer, and the alternative is a silent reordering by nothing at all.
    """
    kept = [r for r in (rows or []) if isinstance(r, Mapping)]
    if len(kept) < 2 or not any(
            any(r.get(k) for k in ("date", "status", "country", "disambiguation",
                                   "medium_formats", "media", "track_count"))
            for r in kept):
        # Nothing stored states a fact any rule could reorder by (every list
        # written before the rows carried their facts) — the order stands.
        return kept
    return [kept[c.index] for c in rank_releases({}, kept, cfg)]


def _score(levels):
    """The tier tuple as one number in [0, 1) — see `_SCORE_BASE`."""
    total, place = 0.0, 1.0
    for level in levels:
        place /= _SCORE_BASE
        total += int(round(level * (_SCORE_BASE - 1))) * place
    return round(total, 4)


def _deciding_reason(levels, other_levels, other):
    """The tier the winner actually won on, said out loud.

    `reasons` otherwise lists the winner's own facts; this is the one sentence
    that names WHY it beat the runner-up, which is what a user asks when two
    editions look alike. For the rules a reader cannot see on the loser — that
    the winner is the EARLIEST rather than merely first, that its fuller date
    is what beat the other, that the other's comment is what lost it — the
    tier's own substance travels with the sentence (`_TIER_NOTES`).
    """
    for i, (mine, theirs) in enumerate(zip(levels, other_levels)):
        if mine != theirs:
            tier = _TIER_NAMES[i]
            note = _TIER_NOTES.get(tier)
            return (f'ranked above "{other.title}" ({other.release_mbid}) on '
                    f"{_TIER_LABELS[tier]}"
                    + (f" — {note}" if note else ""))
    return (f'tied with "{other.title}" ({other.release_mbid}) on every rule — '
            "MusicBrainz's own order kept")


def choose_release(release_group, releases, cfg=None, *, strict=False,
                   primary_type="", secondary_type="", wanted_types=None):
    """The best edition per `rank_releases`, or None when nothing qualifies.

    The caller's type filter always applies — a single is never returned for an
    album request. With `strict` the config's eligibility rules apply too, so
    the unattended paths (auto-import, the watch) can never take a promotion, a
    bootleg or a country-less edition; without it those editions are still
    ranked and the pick can be one, which is what a report to the user shows.
    """
    ranked = rank_releases(release_group, releases, cfg, strict=strict,
                           primary_type=primary_type, secondary_type=secondary_type,
                           wanted_types=wanted_types)
    return pick(ranked, strict)


def pick(ranked, strict=False):
    """The first candidate of an already-ranked list, or None.

    `strict` skips the editions the config's rules forbid an unattended
    download from taking (`Candidate.eligible`); every candidate the caller's
    own type filter excluded is skipped either way.
    """
    for cand in ranked:
        if not cand.type_ok:
            continue
        if cand.eligible or not strict:
            return cand
    return None


def choice_payload(release_group_mbid, release_group, releases, cfg=None, *,
                   prefer="", strict=False, primary_type="", secondary_type="",
                   wanted_types=None):
    """The `/api/mb/release-choice` body: the group, the pick, the alternatives
    and the policy that ranked them — one place, so the route, the watch and
    the tests all describe a pick the same way.

    `prefer` forces a release the caller named (an explicit override): it is
    returned as `chosen` with its reasons saying so, and `chosen` is None when
    the id is not among the group's editions.
    """
    ranked = rank_releases(release_group, releases, cfg, strict=strict,
                           primary_type=primary_type, secondary_type=secondary_type,
                           wanted_types=wanted_types)
    group = release_group if isinstance(release_group, Mapping) else {}
    wanted = str(prefer or "").strip().lower()
    chosen = None
    if wanted:
        for cand in ranked:
            if cand.release_mbid.lower() == wanted:
                chosen = replace(cand, reasons=cand.reasons + (
                    f"chosen because you asked for this release ({cand.release_mbid})",))
                break
    else:
        chosen = pick(ranked, strict)
    if chosen is not None and len(ranked) > 1 and not wanted:
        others = [c for c in ranked if not (c.eligible and c.type_ok)]
        if others and sum(1 for c in ranked if c.eligible and c.type_ok) == 1:
            chosen = replace(chosen, reasons=chosen.reasons + (
                f"the only eligible edition ({len(others)} other(s) skipped)",))
    primary, secondary = group_types(group)
    result = {
        "release_group_mbid": str(release_group_mbid or ""),
        "release_group": {
            "title": str(group.get("title") or ""),
            "first_release_date": first_release_date(group),
            "primary_type": primary,
            "secondary_types": list(secondary),
            "track_count": expected_tracks(group, releases),
        },
        "chosen": chosen.to_dict() if chosen is not None else None,
        "candidates": [c.to_dict() for c in ranked[:CANDIDATE_LIMIT]],
        "policy": policy_report(cfg),
    }
    return result
