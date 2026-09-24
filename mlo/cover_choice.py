"""The ONE cover-choice policy: which cover candidate is the BEST cover.

Every surface that needs that answer asks this module — the import chain's
cover step (`server.imports.run_cover_step`), the wizard's Covers step and the
album page's cover search — so the image an unattended import lands, the image
the wizard offers first and the image the finder lists first are the same pick
by the same rules, and each of them can say WHY.

It is a pure ranking over candidate dicts plus a config dict: no network, no
config file, no cache, no state, so the same candidates and the same config
always produce the same pick. The candidates are the rows the cover finder
already deals in (`server.integrations.cover_search`), each carrying what it
was MEASURED from rather than what its name suggests:

    source          provider id ("applemusic", "musicbrainz", "deezer", ...)
    big / small     the image URLs (``big`` is what would be written)
    width / height  the REAL pixel size, read from the image's own header
                    bytes (probed) — ``None`` means unknown, never a guess
    format          the container read from those same bytes ("" = unknown)
    bytes           how many bytes that probe returned (0 = an empty answer)
    front           whether the provider labels it the front cover
    kind            the provider's own type: "front" / "back" / "other" / ""
    release_cover   False for the release-GROUP's image (the album's own art —
                    the reference, ranked first), True for one release's own
                    front cover, None unknown
    rank            the provider's own order for it (0 = its first answer)
    title / artist  the SOURCE's own statement about which release the image
                    belongs to (COV's `releaseInfo`, Deezer's and iTunes' own
                    album objects), and what the row says about it
    tracks          how many tracks that stated release has, or None

`identity=` is the OTHER side of that last pair: the album being covered (its
artist, its title, its track count), which rule 2 reads and the payload hands
back so a caller can see what a candidate set was judged against.

The rules, in the order they decide. Each is a tier weighted so heavily that
no lower tier can ever outvote a higher one, which is why the score IS the
order (the same positional encoding `mlo.release_choice` uses, base 9):

1. release   the release GROUP's front cover beats one release's own. The group
             image is the album's own art — it is the reference the finder shows
             beside the candidates (`CoverSearchModal`'s `caaRef`) and what the
             automatic search is compared against — while a `/release/<id>/front`
             is a single edition's cover (a reissue, a promo sleeve) and ranks
             below even a name-searched row. Unknown (a name-searched row that
             states neither) sits between.
2. identity  the row's own release must BE this album. The artist and the
             title it states are compared with the album's own — a row that
             contradicts them (a karaoke or tribute album carrying the same
             title, another album by the same artist) is REJECTED, not merely
             outranked, and a row with the right names but a different track
             count (a reissue, a compilation — the artwork is usually the
             same) ranks below one whose tracklist matches. A row that states
             nothing is not punished for it: it cannot be checked, so it sits
             in the middle like every other unknown in this policy.
3. kind      the front cover beats the back/other images a provider labels.
4. size      the larger decoded SHORTER side up to the configured target
             (`cover_target_size`), and no credit past it: a file already at
             the target beats a 3000px one that is only ever downscaled, and
             nothing is rewarded for being enlarged. An UNKNOWN size is
             neither rewarded nor blamed. This is the same law the image
             rules use elsewhere in this app — undersized is acceptable
             (nothing is ever upscaled), oversized is the problem.
5. source    the configured `cover_sources` order (the shipped default is
             `DEFAULT_SOURCE_ORDER`); a source the order does not name ranks
             after every configured one.
6. format    JPEG (the library's own cover format, `cover_jpeg_quality`) beats
             WebP/PNG/other, which are re-encoded when written.
7. square    the configured cover aspect — a non-square image is centre-cropped
             by the writer, so it loses pixels; the threshold is the same
             `cover_crop_threshold` / `grader_strict_square_threshold` pair the
             grader judges with.
8. quality   an obviously re-compressed thumbnail, and above all an UPSCALED
             one (a URL that asks a CDN for 250px and answers 1000x1000 has
             invented its extra pixels), ranks below a clean full-size image.
9. rank      the provider's own order — the last tiebreak, and nothing else.

Rejection is separate from ranking, and a rejected candidate is still REPORTED
with the reason it was rejected (never silently dropped, and never silently
substituted by a worse one):

* a provider that stated an error,
* no image URL at all,
* an empty answer (0 bytes),
* bytes that are not a decodable JPEG/PNG/WebP image,
* a row whose own release contradicts the album — a different artist, or a
  different album by the same artist (rule 2): the wrong album's art must
  never be the automatic pick, however big or pretty it is,
* a shorter side below the FLOOR — the same number `server.main._cover_metrics`
  calls "the minimum" and the grader enforces: `cover_target_size` while
  `cover_resize_enabled` is on, and no floor at all when it is 0. That floor
  covers an UNKNOWN size too: while one is configured, a row whose image was
  never measured (the probe failed, the row is past `COVER_PROBE_LIMIT`, the
  host is not public) has no evidence it can reach the floor, so it is listed
  and can be applied by hand but is never the automatic pick.

The floor removes a candidate from the AUTOMATIC pick only: it stays in the
ranked list, with its own reason, and a user can still apply it by hand. So an
album whose every candidate is 500px is reported as one no source could cover
at the library's own size, rather than being quietly given the smallest image
the internet had.

`choose_covers` returns ``(chosen, ranked, notes)``: the winner (or None when
every candidate was rejected), every candidate ranked best-first with the
reasons that put it there — the winner carrying the one sentence that says why
it beat the runner-up, every loser carrying the reason it lost — and `notes`,
the lines that must not be left implicit: what each source did (answered, had
nothing, refused, or was skipped and why: a missing key is stated, never
worked around) and why nothing could be chosen when nothing could.

`SEARCH_LIMIT` is the ONE number of candidates a search ASKS the finder for:
the surfaces above all ask it (the dialog's route default, `cover_search`'s own
default and `imports.COVER_REVIEW_LIMIT`), because the finder truncates its
answer at the ask — two callers asking for different amounts are ranking
different candidate sets, and the unattended one could then land an image the
dialog never even saw. `CANDIDATE_LIMIT` is the other side of that: how many of
the ranked rows a response carries.
"""
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Mapping, Optional, Sequence

# Candidates a response lists. Everything is still RANKED in full — only the
# response is capped, so a page never carries hundreds of rows.
CANDIDATE_LIMIT = 20

# Candidates a search ASKS the finder for. ONE number for every surface that
# asks — the cover finder's own dialog (`server.main`'s `/api/cover/search`)
# and the unattended import / add-to-library path
# (`server.imports.cover_candidates`) — because the finder TRUNCATES its answer
# at what it is asked for (`server.integrations._cov_results` stops at `limit`):
# two callers asking for different numbers are ranking different candidate sets,
# and the unattended one could then land an image the dialog never even saw.
# This is the ASK; `CANDIDATE_LIMIT` above is what the ANSWER carries.
SEARCH_LIMIT = 40

# The shipped default source order, best first. One place: the finder's own
# catalogue order (`server.integrations.COV_SOURCE_PRIORITY`) is this list, so
# the picks and the picker cannot disagree. The order is the app's own quality
# judgement — Qobuz/Apple/Tidal/Bandcamp publish full-resolution artwork,
# Deezer serves at most 1000px, Spotify's covers are re-encoded, and Discogs
# and MusicBrainz/CAA carry what users uploaded.
DEFAULT_SOURCE_ORDER = ("qobuz", "applemusic", "tidal", "bandcamp", "deezer",
                        "spotify", "itunes", "discogs", "musicbrainz")

# The scoring is a positional encoding of the tier tuple, base 9, most
# significant tier first — a bigger score IS a better pick and no lower tier
# can outvote a higher one (see mlo.release_choice, which this mirrors). The
# base is the number of tiers, so a new rule raises it and the three tuples
# below together — never one of them alone.
_SCORE_BASE = 9
_TIER_NAMES = ("release", "identity", "kind", "size", "source", "format",
               "square", "quality", "rank")
# What a tie-break sentence calls each tier.
_TIER_LABELS = {
    "release": "the album's own cover (the release group's art)",
    "identity": "the album-identity check",
    "kind": "the front-vs-other type",
    "size": "the image size",
    "source": "the configured source order",
    "format": "the image format",
    "square": "the square-aspect rule",
    "quality": "the thumbnail/upscale check",
    "rank": "the provider's own order",
}

# The container ladder: the library re-encodes every cover to JPEG at
# `cover_jpeg_quality` (or keeps PNG when the image really has alpha), so a
# JPEG is already the shape it will be stored in and anything else is a second
# generation. "" (never probed) is neither rewarded nor blamed.
_FORMAT_LEVELS = {"jpeg": 1.0, "jpg": 1.0, "jpe": 1.0, "webp": 0.7,
                  "png": 0.6, "gif": 0.3, "bmp": 0.3, "": 0.8}

# The size a URL itself asks a CDN for — a HINT, and only ever used to spot a
# thumbnail or an upscale, never as a candidate's own size. The shapes this
# app's providers actually serve: Apple's `/100x100bb.jpg` (and the
# `/3000x3000bb.jpg` this app rewrites it to — the artwork path names the size
# it serves), Deezer's `/<size>x<size>-…jpg`, and the Cover Art Archive's two
# pre-rendered thumbnail forms (`/front-250`, `<image id>-1200.jpg`).
_APPLE_HINT_RE = re.compile(r"/(\d{2,5})x(\d{2,5})bb\.", re.I)
_CDN_HINT_RE = re.compile(r"/(\d{2,5})x(\d{2,5})-\d", re.I)
_CAA_FRONT_HINT_RE = re.compile(r"/(?:front|back)-(\d{2,5})(?:\.\w+)?(?:$|[?#])", re.I)
_CAA_IMAGE_HINT_RE = re.compile(r"/\d+-(\d{2,5})\.(?:jpe?g|png)(?:$|[?#])", re.I)
_CAA_THUMBS_RE = re.compile(r"/thumbnails/(\d{2,5})/", re.I)
# A request under half the cover target is a thumbnail by any measure (CAA's
# 250/500px forms); a request at 80% of the target is a source's own maximum
# (Deezer serves 1000px, and that is all it has), so it is not penalised for it.
_THUMBNAIL_FRACTION = 0.5
# An upscale is "bigger than the size its own URL asked for", with a little
# room for the 1px rounding every CDN does.
_UPSCALE_TOLERANCE = 0.02
# What a cover "should" be when no target is configured, for the thumbnail test
# above only: the app's own shipped cover size (`cover_target_size`).
_SHIPPED_TARGET = 1200

_RULES = (
    "the release group's front cover — the album's own art — beats one "
    "release's own cover",
    "a candidate has to BE this album: a row whose own release names another "
    "artist or another album is rejected, a row whose tracklist disagrees "
    "ranks below one that matches, and a row that states nothing about its "
    "release is neither rewarded nor blamed for it",
    "a front cover beats the back/other images a provider labels",
    "the larger decoded side wins up to the configured target — an oversized "
    "or upscaled file is never rewarded over a clean one at the target size",
    "the configured cover_sources order decides after the size",
    "JPEG (the library's own cover format) beats WebP/PNG/other, which are "
    "re-encoded when they are written",
    "a square image beats one the writer would have to centre-crop",
    "a re-compressed thumbnail — and above all an upscaled one — ranks below "
    "a clean full-size image",
    "the provider's own order only ever breaks a tie",
    "a candidate below the cover target (the minimum server.main._cover_metrics "
    "reports and the grader enforces) is rejected, not silently ranked last — "
    "and so is one whose size was never measured while that minimum is set",
)


# Provider ids that name the same catalogue under two spellings: our own
# identity read asks the Cover Art Archive directly, and the meta-search serves
# exactly that archive as its "musicbrainz" source — so a configured order
# treats them as one, instead of ranking the release's own cover as an unknown
# source. Deezer and iTunes happen to share one spelling already.
_SOURCE_ALIASES = {"coverartarchive": "musicbrainz"}


def _source_of(row):
    """The candidate's source id, in the configured order's own vocabulary."""
    source = str(row.get("source") or "").strip().lower()
    return _SOURCE_ALIASES.get(source, source)


def _int(value):
    """A positive int from a payload field, else None (never 0-by-accident)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def url_size_hint(url):
    """The pixel size a URL ASKS for, or None.

    A request hint, not a promise: a CDN may answer any size for the same URL
    (Apple's `/100x100bb.jpg` and `/3000x3000bb.jpg` are one asset). It is read
    only to spot a thumbnail or an upscaled one, and a URL that states no size
    reads None rather than being guessed at.
    """
    text = str(url or "")
    if not text:
        return None
    if "coverartarchive.org" in text.lower():
        for rx in (_CAA_FRONT_HINT_RE, _CAA_IMAGE_HINT_RE, _CAA_THUMBS_RE):
            m = rx.search(text)
            if m:
                return int(m.group(1))
    for rx in (_APPLE_HINT_RE, _CDN_HINT_RE):
        m = rx.search(text)
        if m:
            return max(int(m.group(1)), int(m.group(2)))
    return None


def _format_of(row):
    """The candidate's container, lowercased and normalised ("jpg" -> "jpeg")."""
    fmt = str(row.get("format") or "").strip().lower().lstrip(".")
    if fmt in ("jpg", "jpe"):
        return "jpeg"
    return fmt


def policy_config(cfg=None):
    """The config the policy reads, with the shipped defaults filled in.

    A partial dict (a test, the CLI) gets the defaults for everything it does
    not state, so `cover_target_size`, the source order and the aspect
    threshold each live exactly once — in `mlo.config.DEFAULT_CONFIG`.
    """
    from mlo.config import DEFAULT_CONFIG

    merged = dict(DEFAULT_CONFIG)
    if isinstance(cfg, Mapping):
        merged.update({k: v for k, v in cfg.items() if v is not None})
    order = [str(x).strip().lower() for x in (merged.get("cover_sources") or [])
             if str(x).strip()]
    merged["cover_sources"] = order or list(DEFAULT_SOURCE_ORDER)
    target = _int(merged.get("cover_target_size")) or 0
    merged["cover_target_size"] = max(0, min(4000, target))
    # The FLOOR: the same number the write path calls the minimum and the
    # grader enforces. Only a real target under an enabled resize is a floor —
    # with no target there is nothing a candidate could fall short of.
    resize = bool(merged.get("cover_resize_enabled", True))
    merged["cover_minimum"] = merged["cover_target_size"] if resize else 0
    # The squareness threshold: the strict pair when the cover is forced to an
    # exact size, the general crop threshold otherwise — exactly the choice
    # mlo.grader makes for the same two keys.
    try:
        if merged.get("cover_force_exact_size", True):
            thr = float(merged.get("grader_strict_square_threshold", 0.0) or 0.0)
            thr = max(0.0, min(0.05, thr))
        else:
            thr = float(merged.get("cover_crop_threshold", 0.0) or 0.0)
            thr = max(0.0, min(0.5, thr))
    except (TypeError, ValueError):
        thr = 0.0
    merged["cover_square_threshold"] = thr
    merged["cover_enforce_square"] = bool(merged.get("cover_enforce_square", True)
                                          and merged.get("grade_check_cover_crop", True))
    return merged


def policy_report(cfg=None):
    """The policy as data — the Settings keys, the ladder and the rules."""
    conf = policy_config(cfg)
    return {
        "target": conf["cover_target_size"],
        "minimum": conf["cover_minimum"],
        "sources": list(conf["cover_sources"]),
        "square_threshold": conf["cover_square_threshold"],
        "enforce_square": conf["cover_enforce_square"],
        "jpeg_quality": conf.get("cover_jpeg_quality", 90),
        "rules": list(_RULES),
    }


@dataclass(frozen=True)
class Candidate:
    """One scored candidate, with the facts that put it where it is.

    `score` is 0..1, higher is better, and it encodes the tier order (see the
    module docstring): a bigger score IS a better pick, and `chosen` is the
    first candidate that was not rejected. `rejected` is "" for a candidate
    that could be a cover at all, else the sentence naming why it cannot —
    it is listed, and can still be applied by hand, but it is never the
    automatic pick.
    """
    source: str = ""
    url: str = ""
    small: str = ""
    title: str = ""
    artist: str = ""
    tracks: Optional[int] = None
    page: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    format: str = ""
    bytes: Optional[int] = None
    front: Optional[bool] = None
    kind: str = ""
    release_cover: Optional[bool] = None
    rank: int = 0
    index: int = 0
    side: Optional[int] = None
    score: float = 0.0
    reasons: tuple = ()
    rejected: str = ""

    def to_dict(self):
        """The row as the API/UI reads it: the finder's own keys (`small`,
        `big`) plus the policy's verdict."""
        return {
            "source": self.source, "small": self.small or None, "big": self.url or None,
            "title": self.title or None, "artist": self.artist or None,
            "tracks": self.tracks, "url": self.page or None,
            "width": self.width, "height": self.height,
            "format": self.format or None, "bytes": self.bytes,
            "front": self.front, "kind": self.kind or None,
            "release_cover": self.release_cover, "rank": self.rank,
            "score": self.score, "reasons": list(self.reasons),
            "rejected": self.rejected or None,
        }


@dataclass(frozen=True)
class _Context:
    """Everything the rules need that is not the candidate itself."""
    order: tuple
    target: int
    minimum: int
    square_threshold: float
    enforce_square: bool
    # The album being covered, as `identity=` handed it over: what the rows are
    # checked against (rule 2). All three may be unknown — an album with no
    # tags and no marker is searched for by whatever it has, and nothing is
    # then verified rather than everything being rejected.
    artist: str = ""
    album: str = ""
    tracks: Optional[int] = None


def _url_of(row):
    return str(row.get("big") or row.get("small") or "").strip()


def _fold(text):
    """The app's comparison key for a title or an artist.

    `server.discovery.norm`'s own fold — NFKD, the combining marks dropped,
    case folded, anything that is not a letter or a digit collapsed to a space
    — repeated here because this module is the engine's policy and imports no
    server code (the same reason `mlo.lyrics_providers` carries its own). The
    two must agree, or the app would have two opinions about when two names are
    the same string; `tools/test_cover_choice.py` checks them against each
    other for exactly that reason.
    """
    folded = "".join(ch for ch in unicodedata.normalize("NFKD", str(text or ""))
                     if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", folded.casefold()).strip()


def _same_name(a, b):
    """Whether two FOLDED names are the same release's.

    Equality, or one being a whole PHRASE of the other — a provider spells a
    credit its own way ("Radiohead, レディオヘッド*"), a reissue spells its title
    its own way ("OK Computer OKNOTOK 1997 2017"), and a correct row thrown out
    for the spelling is the one failure this comparison must not have. The
    comparison is word-bounded rather than a bare substring: an artist really
    called "A" (or an album called "T") would otherwise match every name that
    happens to contain that letter. Containment can still be generous — it
    accepts an artist credit that merely mentions the real one — which is why
    the track count is read too; a clearly different artist or album, the case
    that rejects, matches neither way.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return f" {short} " in f" {long} "


def _identity_verdict(row, ctx):
    """The row's own release against THIS album: ``(verdict, reason)``.

    A name search answers with the album AND with everything that is called
    like it: a karaoke or tribute album carrying the same title, an 8-bit
    rendition, another album by the same artist, a compilation. The row says
    which release it is (COV's `releaseInfo`, Deezer's and iTunes' own album
    objects), so it can be checked instead of hoped about:

    ``"same"``     the row's release states this album's artist and title (and
                   a track count that agrees, or none);
    ``"unknown"``  it states too little to tell — a source that says nothing
                   is NOT punished for it, it sits in the middle the way every
                   other unknown in this policy does;
    ``"count"``    the right artist and title but a different tracklist: a
                   different edition (a reissue, a compilation), whose artwork
                   is usually the same — ranked below a matching row, never
                   rejected;
    ``"other"``    it states something that CONTRADICTS the album — a
                   different artist, or a different album by the same artist.
                   `_rejection` turns this into a rejection: the wrong album's
                   art must never be the automatic pick.

    Only fields BOTH sides state are compared, so a half-known album (its
    artist off the folder name, say) cannot condemn a row over a name it never
    really had.
    """
    want_artist, want_album = _fold(ctx.artist), _fold(ctx.album)
    if not want_artist and not want_album:
        return "same", ""                 # no identity to check a row against
    got_artist = _fold(row.get("artist"))
    got_album = _fold(row.get("title"))
    if got_artist and want_artist and not _same_name(got_artist, want_artist):
        return "other", (f"the source's own release says “{row.get('artist')}”, "
                         f"not “{ctx.artist}” — a different artist's release")
    if got_album and want_album and not _same_name(got_album, want_album):
        return "other", (f"the source's own release is “{row.get('title')}”, "
                         f"not “{ctx.album}” — a different album")
    tracks = _int(row.get("tracks"))
    if tracks and ctx.tracks and tracks != ctx.tracks:
        return "count", (f"the source's release has {tracks} track(s) where "
                         f"this album has {ctx.tracks} — a different edition, so "
                         f"it ranks below one whose tracklist matches")
    stated = [str(row.get(k)).strip() for k in ("artist", "title")
              if str(row.get(k) or "").strip()]
    checked = [got for got, want in ((got_artist, want_artist),
                                     (got_album, want_album)) if got and want]
    if not checked:
        return "unknown", ("the source states no artist or title — the row "
                           "cannot be checked against this album")
    return "same", "the source's own release: " + " — ".join(stated)


def _identity_level(verdict, why):
    """(level, reason) for rule 2 — how well the row's own release matches.

    `why` is the sentence `_identity_verdict` produced, so the winner's facts
    and every loser's reason say what the row IS, not merely that it was
    checked.
    """
    if verdict == "same":
        return 1.0, why
    if verdict == "count":
        return 0.2, why
    return 0.6, why               # "unknown": it cannot be checked at all


def _rejection(row, url, side, ctx, identity="same", identity_why=""):
    """Why this candidate cannot be a cover at all, or "" when it can.

    Checked before anything is scored: a candidate the finder itself failed on
    (a provider error), one with no URL to write, one the probe found nothing
    at all behind, one whose bytes are not an image this app can decode, one
    whose own release CONTRADICTS the album (`identity` "other" — a karaoke,
    tribute or other-album row), and one below the floor — the target the write
    path and the grader both call the minimum, which nothing here may quietly
    step around. That floor covers an UNKNOWN size as well: while one is
    configured, a row whose image was never measured has no evidence it can
    reach it, so it may be listed and applied by hand but never picked here.

    The two probe verdicts (0 bytes, bytes that are not an image) only ever
    REJECT a candidate the probe is the only evidence about: a provider that
    stated the image's size has already proved there is an image there, and a
    probe that came back empty is a failed enrichment, not a candidate that
    cannot be a cover.
    """
    err = str(row.get("error") or "").strip()
    if err:
        return err
    if not url:
        return "no image URL to fetch"
    nbytes = row.get("bytes")
    if nbytes is not None:
        try:
            nbytes = int(nbytes)
        except (TypeError, ValueError):
            nbytes = None
    if side is None:
        if nbytes == 0:
            return "the image URL answered 0 bytes"
        if nbytes and not _format_of(row):
            return (f"{nbytes} bytes that are not a JPEG/PNG/WebP image — the "
                    f"URL did not answer with cover art")
    if identity == "other":
        return f"{identity_why} — it is not this album's cover"
    if ctx.minimum > 0:
        if side is None:
            return (f"the image was never measured, and this library's "
                    f"{ctx.minimum}×{ctx.minimum} minimum means nothing "
                    f"unmeasured can be shown to reach it")
        if side < ctx.minimum:
            return (f"{side}×{side} is below the minimum {ctx.minimum}×"
                    f"{ctx.minimum} — nothing is ever upscaled, so it can never "
                    f"reach the cover target")
    return ""


def _release_level(row):
    """(level, reason) for rule 1 — the album's own art over one edition's.

    The REFERENCE is the release-group's image: that is the cover the finder
    shows the candidates beside (`CoverSearchModal`'s `caaRef`), the one the
    automatic search is judged against, and the album's art rather than one
    pressing's. A `/release/<id>/front` URL is that single release's own cover
    — a different edition's art, a promo sleeve, a reissue — so it ranks below
    a name-searched row, which at least does not claim to be this album's
    specific edition either. (This used to be inverted, which is how an
    automatic search could pick an image that was NOT the reference the user
    was comparing it with.)
    """
    got = row.get("release_cover")
    if got is False:
        return 1.0, ("the release group's front cover — this album's own art, "
                     "the image the candidates are compared against")
    if got is True:
        return 0.2, ("one release's own front cover — that edition's art, not "
                     "necessarily this album's")
    return 0.6, "a name-searched cover — the source does not say whose release it is"


def _kind_level(row):
    """(level, reason) for rule 3 — front beats the types a provider labels."""
    kind = str(row.get("kind") or "").strip().lower()
    front = row.get("front")
    if front is None and kind:
        front = kind == "front"
    if front is True or kind == "front":
        return 1.0, "labelled the front cover"
    if front is False or kind in ("back", "other", "other ", "medium", "booklet"):
        return 0.2, f"labelled {kind or 'not the front cover'}"
    return 0.6, "the provider does not label its type"


def _size_level(size, ctx):
    """(level, reason) for rule 4 — bigger up to the target, never past it.

    The shorter side is what is measured: the writer centre-crops to a square,
    so the shorter side is the one that survives. Below the target the level
    falls off in proportion ("accepted, nothing is ever upscaled, and ranked
    below a file at the target"); past it, the level falls too — a 3000px file
    is only ever downscaled, so it is never better than one already at the
    target, and an absurd one costs more of the tier. With no target
    configured, the shipped cover convention stands in as the reference for
    this preference ONLY (nothing is rejected for it — that is `minimum`'s job).
    """
    side = min(size) if size else None
    if side is None:
        return 0.5, "size unknown (the image was never probed)"
    target = ctx.target if ctx.target > 0 else _SHIPPED_TARGET
    tail = ("" if ctx.target > 0 else
            f" (no cover target is configured — the shipped {_SHIPPED_TARGET}px "
            f"cover convention stands in)")
    if side == target:
        return 1.0, f"{side}×{side} — exactly the {target}px cover target{tail}"
    if side > target:
        # Downscaled, never upscaled: an oversized file is not better than one
        # already at the target, and an absurd one costs more of the tier.
        over = min(0.5, 0.5 * (side - target) / target)
        return (1.0 - over,
                f"{side}×{side} is {side - target}px over the {target}px "
                f"cover target{tail} — it is only ever downscaled, so a file "
                f"already at the target wins")
    return (side / target,
            f"{side}×{side} is {target - side}px short of the {target}px "
            f"target{tail} — accepted (nothing is upscaled), and ranked below a "
            f"file at the target")


def _source_level(source, ctx):
    """(level, reason) for rule 5 — the configured source order."""
    if source in ctx.order:
        i = ctx.order.index(source)
        return ((len(ctx.order) - i) / len(ctx.order),
                f"{source} — preferred source (order {i + 1} of {len(ctx.order)})")
    return 0.0, f"{source or 'no source'} — not in the configured cover source order"


def _format_level(row):
    """(level, reason) for rule 6 — the container the cover will be stored in."""
    fmt = _format_of(row)
    if fmt == "jpeg":
        return 1.0, "JPEG — the library's own cover format"
    if not fmt:
        if row.get("bytes") == 0:
            return (0.8, "the URL answered nothing when it was probed — the "
                         "size is the provider's own")
        return 0.8, "the container was not read (the image was not probed)"
    if fmt == "png":
        return _FORMAT_LEVELS["png"], ("PNG — re-encoded to JPEG when it is written "
                                       "(cover_jpeg_quality)")
    if fmt == "webp":
        return _FORMAT_LEVELS["webp"], "WebP — re-encoded to JPEG when it is written"
    return (_FORMAT_LEVELS.get(fmt, 0.3), f"{fmt.upper()} — not the library's cover format")


def _square_level(row, ctx, size):
    """(level, reason) for rule 7 — the aspect the writer would have to crop."""
    if not ctx.enforce_square:
        return 1.0, ""
    if not size or not size[1]:
        return 1.0, ""
    w, h = size
    if not h:
        return 1.0, ""
    deviation = abs(w / float(h) - 1.0)
    if deviation <= ctx.square_threshold:
        return 1.0, ""
    side = min(w, h)
    lost = max(w, h) - side
    return (max(0.0, 1.0 - deviation),
            f"{w}×{h} is not square ({w / float(h):.3f}:1, threshold "
            f"{ctx.square_threshold:.1%}) — the writer centre-crops it, losing "
            f"{lost}px off the long side")


def _quality_level(row, size, ctx):
    """(level, reason) for rule 8 — a re-compressed or upscaled thumbnail.

    Two things are judged, both from what the URL asks the CDN for versus what
    the URL actually answers with:

    * an UPSCALE — the URL asks for 250px and the file decodes 1000x1000, so
      its extra pixels were interpolated rather than photographed (the same
      verdict `mlo.grader` reports as ARTIST_IMAGE_UPSCALED for an image this
      app's own writer never enlarged);
    * a thumbnail request well below the cover target — a re-compressed copy
      rather than the original, which loses to a full-size image every time.
    """
    hint = url_size_hint(_url_of(row))
    if hint is None:
        return 1.0, ""
    side = min(size) if size else None
    if side is not None and side > hint * (1.0 + _UPSCALE_TOLERANCE):
        return (0.0,
                f"upscaled: the URL asks for {hint}px and it decodes {side}×"
                f"{side} — the extra pixels are interpolated, not detail")
    target = ctx.target if ctx.target > 0 else _SHIPPED_TARGET
    if hint < target * _THUMBNAIL_FRACTION:
        return (0.5, f"a {hint}px thumbnail URL — a re-compressed copy, not the "
                     f"original")
    return 1.0, ""


def _rank_level(rank):
    """(level, reason) for rule 9 — the provider's own order, bucket by bucket.

    Quantised to the same buckets the score is written in (0.._SCORE_BASE-1),
    so "the first row a provider listed" is a full tier and the last bucket is
    a zero — never more.
    """
    r = max(0, int(rank or 0))
    return (1.0 - min(r, _SCORE_BASE - 1) / (_SCORE_BASE - 1),
            f"the provider's own order: #{r + 1}" if r else "the provider's first answer")


def _evaluate(row, ctx, index):
    """(levels, candidate) for one candidate — the whole policy, in one pass."""
    url = _url_of(row)
    width = _int(row.get("width"))
    height = _int(row.get("height"))
    size = (width, height) if (width and height) else None
    side = min(size) if size else None
    source = _source_of(row)
    rank = row.get("rank")
    rank = index if rank is None else max(0, int(rank))

    identity, identity_why = _identity_verdict(row, ctx)
    rejected = _rejection(row, url, side, ctx, identity, identity_why)
    if rejected:
        return None, Candidate(
            source=source, url=url, small=str(row.get("small") or ""),
            title=str(row.get("title") or ""), artist=str(row.get("artist") or ""),
            tracks=row.get("tracks"), page=str(row.get("url") or ""),
            width=width, height=height, format=_format_of(row),
            bytes=row.get("bytes"), front=row.get("front"),
            kind=str(row.get("kind") or ""), release_cover=row.get("release_cover"),
            rank=rank, index=index, side=side, score=0.0,
            reasons=(), rejected=rejected)

    reasons = []
    level_release, why = _release_level(row)
    reasons.append(why)
    level_identity, why = _identity_level(identity, identity_why)
    if why:
        reasons.append(why)
    level_kind, why = _kind_level(row)
    reasons.append(why)
    level_size, why = _size_level(size, ctx)
    reasons.append(why)
    level_source, why = _source_level(source, ctx)
    reasons.append(why)
    level_format, why = _format_level(row)
    if why:
        reasons.append(why)
    level_square, why = _square_level(row, ctx, size)
    if why:
        reasons.append(why)
    level_quality, why = _quality_level(row, size, ctx)
    if why:
        reasons.append(why)
    level_rank, why = _rank_level(rank)
    reasons.append(why)

    cand = Candidate(
        source=source, url=url, small=str(row.get("small") or ""),
        title=str(row.get("title") or ""), artist=str(row.get("artist") or ""),
        tracks=row.get("tracks"), page=str(row.get("url") or ""),
        width=width, height=height, format=_format_of(row), bytes=row.get("bytes"),
        front=row.get("front"), kind=str(row.get("kind") or ""),
        release_cover=row.get("release_cover"), rank=rank, index=index,
        side=side, reasons=tuple(reasons))
    return ((level_release, level_identity, level_kind, level_size, level_source,
             level_format, level_square, level_quality, level_rank), cand)


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
    naming WHY it beat the runner-up, which is what a user asks when two
    candidates look alike — and `lost` for every loser is its mirror.
    """
    for i, (mine, theirs) in enumerate(zip(levels, other_levels)):
        if mine != theirs:
            return f"ranked above {_label(other)} on {_TIER_LABELS[_TIER_NAMES[i]]}"
    return (f"tied with {_label(other)} on every rule — the provider's own "
            f"order kept")


def _label(cand):
    """"a 1200×1200 applemusic image" — a candidate named by what it is."""
    size = f"{cand.width}×{cand.height}" if (cand.width and cand.height) else "an unmeasured image"
    source = cand.source or "an unnamed source"
    return f"the {size} {source} candidate"


def _lost_reason(levels, winner_levels, winner):
    """Why a ranked LOSER did not win — the deciding tier, from the other side."""
    for i, (mine, theirs) in enumerate(zip(levels, winner_levels)):
        if mine != theirs:
            return f"lost to {_label(winner)} on {_TIER_LABELS[_TIER_NAMES[i]]}"
    return (f"tied with {_label(winner)} on every rule — the provider's own "
            f"order kept")


def rank_covers(rows, cfg=None, *, identity=None):
    """Every candidate of *rows*, scored and ranked best first.

    `identity` is the album the rows are being ranked FOR (``{"artist",
    "album", "tracks"}``); rule 2 checks each row's own release against it, so
    a karaoke or other-album row is rejected instead of outranking the real
    cover. Without it nothing is verified — the picks then rest on what the
    finder said about the images alone.

    Rejected candidates are ranked too — at the END, each carrying the sentence
    that rejected it — because "no cover" must never be the silent answer to
    "these were the candidates". Deterministic: the sort is by the tier tuple,
    then by the order the candidates arrived in, so identical inputs always
    rank identically.
    """
    ctx = _context(cfg, identity)
    scored, rejected = [], []
    for i, row in enumerate(rows or []):
        if not isinstance(row, Mapping):
            continue
        levels, cand = _evaluate(row, ctx, i)
        if levels is None:
            rejected.append(cand)
        else:
            scored.append((levels, cand))
    scored.sort(key=lambda pair: (tuple(-v for v in pair[0]), pair[1].index))
    out = []
    for position, (levels, cand) in enumerate(scored):
        reasons = list(cand.reasons)
        if position == 0 and len(scored) > 1:
            reasons.append(_deciding_reason(levels, scored[1][0], scored[1][1]))
        elif position == 0:
            reasons.append("the only candidate that could be a cover")
        else:
            reasons.append(_lost_reason(levels, scored[0][0], scored[0][1]))
        out.append(replace(cand, score=_score(levels), reasons=tuple(reasons)))
    out.extend(rejected)
    return out


def _context(cfg=None, identity=None):
    conf = policy_config(cfg)
    ident = identity if isinstance(identity, Mapping) else {}
    return _Context(order=tuple(conf["cover_sources"]),
                    target=int(conf["cover_target_size"] or 0),
                    minimum=int(conf["cover_minimum"] or 0),
                    square_threshold=float(conf["cover_square_threshold"] or 0.0),
                    enforce_square=bool(conf["cover_enforce_square"]),
                    artist=str(ident.get("artist") or "").strip(),
                    album=str(ident.get("album") or "").strip(),
                    tracks=_int(ident.get("tracks")))


def pick(ranked):
    """The first candidate that is not rejected, or None."""
    for cand in ranked or ():
        if not cand.rejected:
            return cand
    return None


def source_notes(sources):
    """The lines a source report turns into — every source that was asked,
    refused, had nothing or was SKIPPED, with the reason it states.

    A source that needs a key is a skip like any other: the caller states why
    ("needs an API key", "no release-group id to ask about"), and it is said
    out loud rather than left as a source that silently contributed nothing.
    """
    out = []
    for s in sources or ():
        if not isinstance(s, Mapping):
            continue
        sid = str(s.get("id") or s.get("label") or "a source").strip()
        status = str(s.get("status") or "").strip().lower()
        detail = str(s.get("detail") or "").strip()
        count = s.get("count")
        if status in ("used", "ok", "answered"):
            out.append(f"{sid}: {count} candidate(s)" if count
                       else f"{sid}: answered")
        elif status == "empty":
            out.append(f"{sid}: answered with no covers" + (f" ({detail})" if detail else ""))
        elif status in ("error", "refused"):
            out.append(f"{sid}: refused — {detail or 'the request failed'}")
        elif status in ("skipped", "needs_key"):
            out.append(f"{sid}: skipped — {detail or 'not asked'}")
        elif detail:
            out.append(f"{sid}: {detail}")
    return out


def choose_covers(rows, cfg=None, *, sources=None, identity=None):
    """The best cover of *rows*: ``(chosen, ranked, notes)``.

    `chosen` is None when nothing could be a cover at all (every candidate
    rejected: the notes and the candidates' own `rejected` sentences say why).
    `ranked` is every candidate best-first, the winner's reasons ending in the
    sentence that says why it won and every loser's in the one that says why it
    lost. `notes` is what the sources did plus why nothing was chosen — see
    `source_notes`. `identity` is the album rule 2 checks the rows against.
    """
    ranked = rank_covers(rows, cfg, identity=identity)
    chosen = pick(ranked)
    notes = source_notes(sources)
    kept = [c for c in ranked if not c.rejected]
    rejects = [c for c in ranked if c.rejected]
    if kept:
        notes.append(f"{len(kept)} candidate(s) could be a cover"
                     + (f", {len(rejects)} rejected" if rejects else ""))
    for cand in rejects[:5]:
        notes.append(f"rejected {_label(cand)}: {cand.rejected}")
    if chosen is None:
        notes.append("nothing could be chosen — "
                     + (rejects[0].rejected if rejects
                        else "no source answered with a candidate"))
    return chosen, ranked, notes


def cover_payload(rows, cfg=None, *, sources=None, provider=None, identity=None):
    """The cover-choice body: the pick, the ranked candidates, the notes, the
    policy and the identity they were checked against — one place, so the cover
    step, the API route and the tests all describe a pick the same way.

    ``identity`` echoes the album the rows were ranked for (its artist, its
    album name, its track count), so a staged record or a search response says
    what a candidate had to BE — and an empty one means the rows were ranked on
    what the finder said about the images, with nothing to verify them against.
    """
    chosen, ranked, notes = choose_covers(rows, cfg, sources=sources,
                                          identity=identity)
    ident = identity if isinstance(identity, Mapping) else {}
    return {
        "chosen": chosen.to_dict() if chosen is not None else None,
        "candidates": [c.to_dict() for c in ranked[:CANDIDATE_LIMIT]],
        "candidate_count": len(ranked),
        "rejected_count": len([c for c in ranked if c.rejected]),
        "notes": notes,
        "provider": provider,
        "policy": policy_report(cfg),
        "identity": {"artist": str(ident.get("artist") or ""),
                     "album": str(ident.get("album") or ""),
                     "tracks": _int(ident.get("tracks"))},
    }
