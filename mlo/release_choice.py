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
3. tracks      a release short of the release group's OWN track count is
               penalised, so a 1-track promo can never beat the full album.
4. date        the edition closest to the group's first release date — the
               original, not a reissue or a deluxe — unless a later one is
               materially more complete (tier 3 outranks this one); among
               editions of the same year the one that states its date in full,
               because the album folder is named after it.
5. edition     `prefer_original_edition` (default true): a clean/explicit-edited
               edition sorts below the original — a clean edition may carry
               altered audio.
6. plain       a plain release beats a disambiguated/parenthesised one.
7. country     `prefer_release_country` — a TIE-BREAKER and nothing else.

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
_TIER_NAMES = ("status", "medium", "tracks", "date", "edition", "disambiguation", "country")
# What a tie-break sentence calls each tier (see _deciding_reason).
_TIER_LABELS = {
    "status": "release status",
    "medium": "the medium order",
    "tracks": "the track count",
    "date": "the release date",
    "edition": "the clean/edited-edition rule",
    "disambiguation": "the plain-release rule",
    "country": "the preferred country",
}

# A clean/edited edition: MusicBrainz states this in the release's title or its
# disambiguation comment, never in a field of its own.
_CLEAN_RE = re.compile(r"\bclean\b|\bedited\b|\bcensored\b|\bradio edit\b", re.IGNORECASE)

_RULES = (
    "official beats promotion beats bootleg — an unofficial edition is only "
    "chosen when nothing official exists",
    "the configured medium order decides first: CD, then the other physical "
    "media, digital last",
    "a release short of the release group's own track count is penalised",
    "the original edition beats a later reissue unless the later one is "
    "materially more complete",
    "prefer_release_country only ever breaks a tie",
    "prefer_original_edition prefers the original over a clean/edited edition",
    "a plain release beats a disambiguated one when everything else ties",
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
    """Whether a release group's type is one of the *wanted* names.

    The SAME rule the artist watch applies to what it may queue
    (`server.artist_watch.type_matches`): a group that states secondary types
    is matched by those alone — a live album is selected as "live", not as
    "album" — otherwise by its primary type. An EMPTY selection matches
    nothing: "no type selected" is not a licence to match everything.
    """
    want = {str(t).strip().lower() for t in (wanted or ()) if str(t).strip()}
    if not want:
        return False
    secondary = [str(s).strip().lower() for s in (secondary_types or ()) if str(s).strip()]
    if any(s in want for s in secondary):
        return True
    if secondary:
        return False
    primary = str(primary_type or "").strip().lower()
    return bool(primary) and primary in want


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
            "score": self.score,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class _Context:
    """Everything the rules need that is not the release itself."""
    order: tuple
    country: str
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


def _date_precision(date):
    """How much of the date MusicBrainz states: 1.0 day, 0.66 month, 0.33
    year, 0.0 nothing. The album folder is named after this date, so an
    edition stating only "1983" pins the folder to a year."""
    length = len(str(date or "").strip())
    return {10: 1.0, 7: 0.66}.get(length, 0.33 if length == 4 else 0.0)


def _year(date):
    text = str(date or "").strip()
    try:
        return int(text[:4])
    except (TypeError, ValueError):
        return None


def _release_date_level(date, ctx):
    """(level, reason) for the date tier — the original edition wins.

    The group's first release date is the reference; an edition from that year
    (or earlier — a pressing can predate the group's stated date) is the
    original, and every year of distance costs a fixed step that is larger
    than the whole precision bonus, so an earlier year-only edition still beats
    a later, fully-dated one. An undated edition sorts after every dated one.
    """
    year = _year(date)
    if year is None:
        return (0.0, "no release date on MusicBrainz")
    if ctx.first_year is None:
        return (0.5, f"released {date}" if date else "no release date on MusicBrainz")
    gap = max(0, year - ctx.first_year)
    level = 0.9 * max(0.0, 1.0 - gap / 9.0) + 0.1 * _date_precision(date)
    vague = "" if _date_precision(date) >= 1.0 else " (MusicBrainz states only the year)"
    if not gap:
        reason = (f"original release date {ctx.first_date}" if ctx.first_date
                  else f"earliest edition offered, {date}")
        return (level, reason + vague)
    return (level, f"reissued {date} — {gap} year(s) after the original "
                   f"{ctx.first_date}{vague}")


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

    # 3. completeness ...
    if count <= 0:
        level_tracks = 0.5
        reasons.append("no track count on MusicBrainz — not counted against it")
    elif ctx.expected <= 0:
        level_tracks = 1.0
        reasons.append(f"{count} tracks")
    else:
        level_tracks = min(1.0, count / ctx.expected)
        if count >= ctx.expected:
            reasons.append(f"{count}/{ctx.expected} tracks of the release group")
        else:
            reasons.append(f"{count} of {ctx.expected} tracks — short of "
                           + ("the release group's own count" if ctx.expected_stated
                              else "the fullest edition offered"))

    # 4. date ...
    level_date, date_reason = _release_date_level(date, ctx)
    reasons.append(date_reason)

    # 5. edition kind ...
    clean = bool(_CLEAN_RE.search(f"{title} {disambiguation}"))
    if clean and ctx.keep_original:
        level_edition = 0.0
        reasons.append("clean/edited edition — prefer_original_edition prefers the original")
    else:
        level_edition = 1.0
        if clean:
            reasons.append("clean edition — prefer_original_edition is off, so it is not penalised")

    # 6. plain title ...
    level_plain = 0.0 if disambiguation else 1.0
    if disambiguation:
        reasons.append(f'MusicBrainz disambiguation "{disambiguation}"')

    # 7. country (a tie-breaker, so it is the last tier) ...
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
        disambiguation=disambiguation, reasons=tuple(reasons), eligible=eligible,
        index=index, type_ok=type_ok,
    )
    return ((level_status, level_medium, level_tracks, level_date, level_edition,
             level_plain, level_country), candidate)


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
    first = first_release_date(release_group) if isinstance(release_group, Mapping) else ""
    first_year = _year(first) if first else None
    if first_year is None:
        years = [y for y in (_year(r.get("date")) for r in rows) if y]
        first_year = min(years) if years else None
        first = ""   # no group date: "the earliest edition offered", not "the original"
    ctx = _Context(
        order=tuple(conf["auto_import_medium_order"]),
        country=str(conf.get("prefer_release_country") or "").strip(),
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
    editions look alike.
    """
    for i, (mine, theirs) in enumerate(zip(levels, other_levels)):
        if mine != theirs:
            return (f'ranked above "{other.title}" ({other.release_mbid}) on '
                    f"{_TIER_LABELS[_TIER_NAMES[i]]}")
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
