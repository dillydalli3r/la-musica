"""MusicBrainz / LRCLIB integrations for the import wizard.

MusicBrainz is queried with proper rate limiting (1 req/s) and a UA string
per their API etiquette; LRCLIB answers lyrics. Both are plain public APIs —
nothing here scrapes a site or goes through a challenge.
"""
import asyncio
import contextlib
import html as _html
import json
import os
import random
import re
import threading
import time
import uuid

import httpx

# Importing it installs the ONE shared HTTP client behind the module-level
# `httpx.get`/`httpx.post` helpers this module (and discovery, the playlist
# importer and the AI caller) makes every provider call with — see
# server/httpclient.py. Those helpers stay the call sites' spelling on purpose:
# they are the seam the provider suites replace.
from . import httpclient  # noqa: F401
# The tag vocabulary's own home: the credits module below names the tags its
# identity block and its tag fallback read FROM the registry rather than
# spelling them again, so a renamed tag fails loudly there instead of printing
# an empty header (see IDENTITY_TAG_FIELDS).
from . import tags_registry as _tags_registry

from mlo import cover_choice as _cover_choice
from mlo import release_choice
# The app's own capitalization of a genre name (mlo.genres.display_name): one
# home for it, shared with the writers, the grader and server.discover, so a
# page can never render a genre the way a tag would not hold it.
from mlo.genres import display_name as _genre_display
# The app's DERIVED release types (mlo.naming): a name the app derives from a
# fact MusicBrainz states beside the release-group type — "podcast", read from
# the series relation below — rather than one MusicBrainz publishes.
from mlo.naming import is_derived_type
# The FAMILY a genre name belongs to (mlo.genre_vocab.parent_of), which is what
# genre_category() below buckets by: the fixed filter buckets and the genre
# vocabulary are two different taxonomies, and a name can be spelled one way and
# mean another ("new wave" — a rock movement the keyword table's "wave" rule
# read as electronic). mlo.genre_vocab imports nothing from server/, so this is
# no cycle, and it is the same vocabulary the writers and the grader use.
from mlo.genre_vocab import parent_of as _genre_family, top_of as _genre_head
# The script reading the alias rule is built on (mlo.lyrics_xlit): whether a
# name is written in non-Latin script at all (`non_latin_ratio` /
# `dominant_script`, the pair that decides a lyric transliteration) and the
# threshold below which a text counts as already romanized. `alias_required`
# is the ONE reader outside the grading/transliteration passes, so the alias
# family cannot grow a second, disagreeing notion of "non-Latin".
from mlo.lyrics_xlit import (
    _LATIN_THRESHOLD, dominant_script, non_latin_ratio,
)

MB_BASE = "https://musicbrainz.org/ws/2"
LRCLIB_BASE = "https://lrclib.net/api"
USER_AGENT = "la-musica/2.0 (https://github.com/dillydalli3r/la-musica)"

_last_request = 0.0
_mb_lock = threading.Lock()


class MusicBrainzError(RuntimeError):
    """MusicBrainz did not answer, or refused the request and said why.

    Raised after the retries are exhausted (429/5xx/connection) with a "busy,
    try again" reason, and also when MusicBrainz rejects a request with a 4xx
    whose body names its own reason (a query its search index refused) — that
    message is MusicBrainz's, verbatim, so a caller can show it to the user.

    Typed so a caller can report it as a PER-ITEM reason — "MusicBrainz is
    busy, try again" — instead of a request or a job that sits on an outage.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def mb_reserve_slot():
    """Take this process's MusicBrainz request slot: wait until one second has
    passed since the last MB request made here, then stamp it.

    ONE budget for READS (`mb_get`) and WRITES (`mb_submit._post_rating`):
    MusicBrainz's etiquette is per client, not per verb. The slot is reserved
    BEFORE the request — so two writers queue behind each other instead of
    bursting — and it is the same `_last_request` `mb_get` stamps after its own
    call, so a write and a read can never land inside one second either.
    """
    global _last_request
    with _mb_lock:
        elapsed = time.time() - _last_request
        if elapsed < _MB_MIN_INTERVAL:
            time.sleep(_MB_MIN_INTERVAL - elapsed)
        _last_request = time.time()


_MB_MIN_INTERVAL = 1.0     # MusicBrainz etiquette: one request per second
_MB_RETRY_STATUS = (429, 500, 502, 503, 504)
_MB_BACKOFF_BASE = 1.0     # retry wait: base * 2**(attempt-1), plus jitter
_MB_BACKOFF_MAX = 8.0
# One mb_get — throttle, retries and backoff included — never runs longer than
# this. A 503ing MusicBrainz used to hold its caller (and, for an auto-import
# job, the single-job slot) for as long as the retry loop felt like it.
MB_RETRY_DEADLINE = 45.0


def _raise_mb_refusal(r):
    """MusicBrainz's OWN reason for refusing a request, when it gave one.

    A refused WS/2 request answers 4xx with a JSON body naming the reason —
    {"error": "You submitted a blank search query. You must include a
    non-blank 'query=' parameter with your search."} — which httpx's own
    message ("Client error '400 Bad Request' for url ...") throws away. The
    in-app search box shows MusicBrainz's words for a query MusicBrainz
    rejected, so the body wins whenever it carries one. A 404 is left alone:
    callers branch on it to mean "no such entity", and its body only ever
    says "Not Found".
    """
    if r.status_code == 404:
        return
    try:
        body = r.json()
    except Exception:
        return
    message = str((body or {}).get("error") or "").strip()
    if message:
        raise MusicBrainzError(message, status=r.status_code)


def mb_get(endpoint, params=None, timeout=30.0, retries=3, deadline=MB_RETRY_DEADLINE):
    """Rate-limited MusicBrainz WS/2 GET returning parsed JSON.

    Retries 429/5xx and connection errors with exponential backoff + jitter,
    keeping MusicBrainz's 1 request/second etiquette and a hard overall
    `deadline`; when the retry budget or the deadline runs out it raises
    MusicBrainzError — a typed, reportable reason. Any other status raises
    httpx's own error, except that a 4xx whose body names MusicBrainz's own
    reason raises MusicBrainzError carrying THAT text (`_raise_mb_refusal`) —
    and a 404 keeps meaning "no such entity".
    """
    global _last_request
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    started = time.time()
    attempts = 0
    status = None
    reason = "no response"
    while attempts < max(1, int(retries)):
        attempts += 1
        retry_after = None
        with _mb_lock:
            elapsed = time.time() - _last_request
            if elapsed < _MB_MIN_INTERVAL:
                time.sleep(min(_MB_MIN_INTERVAL - elapsed,
                               max(0.0, deadline - (time.time() - started))))
            try:
                try:
                    r = httpx.get(
                        f"{MB_BASE}/{endpoint}",
                        params=params or {},
                        headers=headers,
                        # never sit on the socket past the deadline either
                        timeout=min(timeout, max(1.0, deadline - (time.time() - started))),
                    )
                except httpx.HTTPError as e:
                    status, reason = None, f"connection error ({e.__class__.__name__})"
                else:
                    if r.status_code not in _MB_RETRY_STATUS:
                        _raise_mb_refusal(r)
                        r.raise_for_status()
                        return r.json()
                    status, reason = r.status_code, f"HTTP {r.status_code}"
                    retry_after = r.headers.get("Retry-After")
            finally:
                # The stamp IS the throttle, so it is set on EVERY way out of
                # this block — the successful one included. Stamping it only on
                # the retry/error path left `_last_request` at 0.0 for a whole
                # run of successful reads, which is MusicBrainz's documented
                # one-request-per-second etiquette turned into "as fast as the
                # socket answers": an artist walk (four pages of release groups
                # plus a browse each) or a rated-library pass fired every
                # request back to back, and MetaBrainz answers that burst with
                # 429/503 — or blocks the user agent. `finally` also covers the
                # 4xx raises below, which cost the same request budget.
                _last_request = time.time()
        if attempts >= retries:
            break
        wait = min(_MB_BACKOFF_BASE * (2 ** (attempts - 1)), _MB_BACKOFF_MAX)
        wait += random.uniform(0.0, wait / 2.0)   # jitter: retries must not sync
        if retry_after:
            try:
                wait = max(wait, float(retry_after))
            except ValueError:
                pass
        if time.time() - started + wait > deadline:
            break
        time.sleep(wait)
    raise MusicBrainzError(
        f"MusicBrainz is busy ({reason}) for {endpoint} after {attempts} "
        f"attempt(s) in {int(time.time() - started)}s — try again in a moment",
        status=status,
    )


# --------------------------------------------------------------------------- #
# Tiny TTL cache for browse endpoints (release/artist pages get re-fetched on
# every navigation; MB etiquette caps us at 1 req/s, so repeat views must not
# re-hit the network). Keyed by (endpoint, sorted params), 10-minute TTL.
# --------------------------------------------------------------------------- #
_BROWSE_CACHE: dict = {}
_BROWSE_LOCK = threading.Lock()
_INFLIGHT: dict = {}
# MB data moves slowly; an hour of TTL keeps repeat views instant (and well
# within the 1 req/s etiquette) without serving anything meaningfully stale.
_BROWSE_TTL = 1800.0


def mb_get_cached(endpoint, params=None, timeout=30.0, retries=5):
    """Cached MB GET with stale-while-revalidate and single-flight.

    A fresh cached copy returns instantly. A stale copy ALSO returns
    instantly while a background thread refreshes it — only genuinely
    unknown payloads block on the rate-limited network, and identical
    concurrent requests share one flight instead of queueing several
    1-second-spaced calls."""
    key = (endpoint, tuple(sorted((k, str(v)) for k, v in (params or {}).items())))
    now = time.time()
    with _BROWSE_LOCK:
        hit = _BROWSE_CACHE.get(key)
        flight = _INFLIGHT.get(key)
    if hit and now - hit[0] < _BROWSE_TTL:
        return hit[1]
    if hit and flight is None:
        # serve stale, refresh in the background
        def _refresh():
            try:
                data = mb_get(endpoint, params, timeout=timeout, retries=retries)
                with _BROWSE_LOCK:
                    _BROWSE_CACHE[key] = (time.time(), data)
            except Exception:
                pass  # keep the stale copy on refresh failure
            finally:
                with _BROWSE_LOCK:
                    ev = _INFLIGHT.pop(key, None)
                if ev is not None:
                    ev.set()  # wake waiters — they read the cache we just wrote

        with _BROWSE_LOCK:
            if _INFLIGHT.get(key) is None:
                _INFLIGHT[key] = threading.Event()
            else:
                return hit[1]  # someone else is already refreshing
        threading.Thread(target=_refresh, daemon=True).start()
        return hit[1]
    if flight is not None:
        # an identical request is already on the wire — wait for it instead
        # of queueing a second rate-limited call behind it
        flight.wait(timeout=45)
        with _BROWSE_LOCK:
            hit = _BROWSE_CACHE.get(key)
        if hit:
            return hit[1]
        raise RuntimeError("concurrent MusicBrainz request did not complete")
    with _BROWSE_LOCK:
        flight = _INFLIGHT.get(key)
        if flight is None:
            flight = _INFLIGHT[key] = threading.Event()
    try:
        data = mb_get(endpoint, params, timeout=timeout, retries=retries)
        with _BROWSE_LOCK:
            # Write the cache BEFORE waking waiters: a woken reader that
            # found an empty cache would 500 a fetch that actually worked.
            _BROWSE_CACHE[key] = (time.time(), data)
            # keep the cache from growing without bound
            if len(_BROWSE_CACHE) > 600:
                for k in list(_BROWSE_CACHE)[:200]:
                    _BROWSE_CACHE.pop(k, None)
    finally:
        with _BROWSE_LOCK:
            _INFLIGHT.pop(key, None)
        flight.set()
    return data


_DETECT_MISSES: dict = {}          # mbid -> when it last failed, for the TTL below
_DETECT_MISS_TTL = 900.0           # a wrong paste is re-probed at most every 15 min


def detect_mbid(mbid):
    """Which MusicBrainz entity kind does this MBID belong to?

    Tries a minimal lookup per browsable entity (cache-shared with the
    entity pages) and reports the first hit — lets the UI route a pasted
    bare ID without the user picking a type. An ID that is not in MB costs
    one request per entity kind, and the search box re-asks while the user
    looks at the answer, so a miss is remembered for a while instead of
    being probed four more times."""
    key = str(mbid or "").lower()
    if key and time.time() - _DETECT_MISSES.get(key, 0.0) < _DETECT_MISS_TTL:
        raise LookupError("no MusicBrainz entity found for this ID")
    for entity in MB_ENTITIES:
        try:
            data = mb_get_cached(f"{entity}/{mbid}", {"fmt": "json"})
        except Exception:
            continue
        return {
            "type": entity,
            "id": data.get("id") or mbid,
            "title": data.get("title") or data.get("name") or "",
        }
    if key:
        if len(_DETECT_MISSES) > 200:
            _DETECT_MISSES.clear()
        _DETECT_MISSES[key] = time.time()
    raise LookupError("no MusicBrainz entity found for this ID")


# MusicBrainz alias `type` values that are not a NAME: "Search hint" aliases are
# the search index's own spellings (misspellings, abbreviations) and belong to
# queries, never to a page a reader is looking at.
_ALIAS_SKIP_TYPES = frozenset({"search hint"})


def _locale_preference(cfg=None):
    """The locale the reader wants names in — `locale`, folded.

    The SAME setting the managed beets import translates names with
    (Settings -> Import & tags: "Preferred locale for aliases"), so a page and
    the files it will produce agree on what an entity is called.
    """
    cfg = cfg if isinstance(cfg, dict) else _release_cfg()
    return str((cfg or {}).get("locale") or "").strip().lower()


def _has_latin(value):
    """Whether *value* carries a Latin letter — a romanization's tell.

    MusicBrainz states a Japanese/Chinese/Korean/Cyrillic name's reading as an
    alias in `*-Latn` or a translation in a Latin-script language; both contain
    Latin letters while the name does not, which is what makes them the useful
    parentheses for a reader who cannot read the script.
    """
    return any("a" <= ch.lower() <= "z" for ch in str(value or ""))


# The script each language the app ships is written in. A name in the
# reader's own script needs no translation, and an alias in it is always
# welcome — this table is what lets `alias_for` tell those two apart
# without a language detector or a network call.
_SCRIPT_CHARS = {
    "ja": r"[\u3040-\u30ff\u4e00-\u9fff]",          # kana or CJK
    "zh": r"[\u4e00-\u9fff]",                          # CJK
    "ko": r"[\uac00-\ud7af]",                          # hangul
    "ru": r"[\u0400-\u04ff]", "uk": r"[\u0400-\u04ff]",
    "bg": r"[\u0400-\u04ff]", "sr": r"[\u0400-\u04ff]",
    "el": r"[\u0370-\u03ff\u1f00-\u1fff]",
    "he": r"[\u0590-\u05ff]",
    "ar": r"[\u0600-\u06ff\u0750-\u077f]",
    "th": r"[\u0e00-\u0e7f]",
    "hi": r"[\u0900-\u097f]", "mr": r"[\u0900-\u097f]", "ne": r"[\u0900-\u097f]",
}


def _reads_natively(value, locale):
    """Whether *value* is written in the script a reader of *locale* reads.

    Everything the table does not name — English included — is read as
    Latin, which is also what a romanization is written in.
    """
    text = str(value or "").strip()
    if not text:
        return False
    lang = str(locale or "").split("-")[0].split("_")[0].lower()
    pattern = _SCRIPT_CHARS.get(lang)
    if pattern is None:
        return bool(_has_latin(text))
    return bool(re.search(pattern, text))


def alias_for(aliases, cfg=None, name=""):
    """The alias to show beside *name* in parentheses, or "".

    An alias must be at least as READABLE as the name it annotates: a
    reader who can already read "Radiohead" gains nothing from its
    Japanese alias (MusicBrainz marks that one primary, which is how
    the pages used to show "Radiohead (レディオヘッド)"), and the mirror
    is worse — romanizing a name for a reader who reads the script it
    is written in. The ladder's answer is therefore dropped when the
    name is already in the reader's script and the alias is not; an
    alias in their script always passes, which is what keeps
    `宇多田ヒカル (Hikaru Utada)` for an English reader and
    `Radiohead (レディオヘッド)` for a Japanese one.

    MusicBrainz states an entity's other-language names as aliases, each with
    its own `locale` and `type`. The ladder, in order:

      1. an alias in the reader's locale flagged `primary` — MusicBrainz's own
         "this is the name in this language",
      2. any other alias in exactly that locale,
      3. the same two again for a COUSIN locale: MusicBrainz separates a script
         with a hyphen (`ja-Latn`) and a region with an underscore (`en_PH`), so
         a reader who asked for `ja` is answered by its romanization and one who
         asked for `en` by `en_PH` — only when the exact locale has nothing,
      4. the entity's `primary` alias whatever its locale — a reader who chose
         no locale, or whose locale this entity has no alias for, still gets
         the name it is also known by rather than a foreign-language guess,
      5. a Latin reading of a name that has none — MusicBrainz states a
         romanization (`*-Latn`) or a translation as an alias, and for a reader
         looking at `ロストアンブレラ` the useful parentheses are "Lost Umbrella";
         a transliteration is preferred over a translation, then MusicBrainz's
         own order decides,
      6. nothing, and the caller shows the name alone.

    A search-hint alias is never chosen, and a value that only differs from the
    name by case or spacing is "no alias": a page must not render "X (X)".
    """
    rows = [a for a in (aliases or []) if isinstance(a, dict)
            and str(a.get("name") or "").strip()
            and str(a.get("type") or "").strip().lower() not in _ALIAS_SKIP_TYPES]
    if not rows:
        return ""
    want = _locale_preference(cfg)
    chosen = _alias_ladder(rows, want, name)
    # An alias must be at least as readable as what it annotates (see the
    # docstring): a reader who reads the name's script gains nothing from a
    # script they do not read, and MusicBrainz's `primary` flag says nothing
    # about the READER. An alias in the reader's own script always passes, so
    # this only ever drops a translation that translates nothing.
    if (want and chosen and _reads_natively(name, want)
            and not _reads_natively(chosen, want)):
        return ""
    return chosen


def _alias_ladder(rows, want, name):
    """Which alias the ladder picks — `alias_for` applies the readability
    rule on top, and everything below is the ladder itself.
    """

    def in_locale(alias):
        got = str(alias.get("locale") or "").strip().lower()
        if not got or not want:
            return False
        if got == want:
            return True
        # MusicBrainz separates a SCRIPT with a hyphen ("ja-Latn") and a REGION
        # with an underscore ("en_PH"), so both are folded before the language
        # prefix is compared: a reader who asked for `ja` wants the `ja-Latn`
        # romanization, and one who asked for `en` wants `en_PH`.
        def base(value):
            return re.split(r"[-_]", value, 1)[0]
        return base(got) == base(want)

    def pick(candidates):
        folded = str(name or "").strip().casefold()
        for alias in candidates:
            value = str(alias["name"]).strip()
            if value.casefold() != folded:
                return value
        return ""

    def is_exact(alias):
        return str(alias.get("locale") or "").strip().lower() == want

    if want:
        # Exact locale before a cousin: a reader who asked for `en` wants the
        # `en` alias, not `en_PH`, and one who asked for `ja` wants the `ja`
        # alias before the `ja-Latn` romanization — the cousins are what is
        # left when the exact one does not exist.
        for candidates in ([a for a in rows if is_exact(a) and a.get("primary")],
                           [a for a in rows if is_exact(a)],
                           [a for a in rows if in_locale(a) and a.get("primary")],
                           [a for a in rows if in_locale(a)]):
            chosen = pick(candidates)
            if chosen:
                return chosen
    chosen = pick([a for a in rows if a.get("primary")])
    if chosen:
        return chosen
    # The last resort, and the one the Japanese/Chinese/Korean pages live on: a
    # Latin reading of a name that has none. `ロストアンブレラ` with no locale
    # configured and no primary alias is still best shown as "Lost Umbrella" —
    # MusicBrainz's own order decides among equals, with a transliteration
    # (`*-Latn`) preferred over a translation.
    if not _has_latin(name):
        latin = [a for a in rows if _has_latin(a.get("name"))]
        latin.sort(key=lambda a: 0 if "latn" in str(a.get("locale") or "").lower() else 1)
        return pick(latin)
    return ""


# The locale whose own alias spelling is the one the app writes when the
# config states none — `locale`'s shipped default (mlo.config.DEFAULT_CONFIG).
DEFAULT_ALIAS_LOCALE = "en"


def alias_locale(cfg=None):
    """The locale the alias TAGS are written for — `locale`, folded, with the
    shipped default when the config states none.

    `_locale_preference` returns "" for "the reader asked for no particular
    language", which is the right answer for `alias_for`'s ladder (it falls
    back to MusicBrainz's own primary alias). A tag, though, is written for
    SOME locale: the app has one (`en` when nothing is configured), and it is
    what decides which suffix spelling a file may carry.
    """
    return _locale_preference(cfg) or DEFAULT_ALIAS_LOCALE


def alias_required(name, cfg=None):
    """Whether *name* needs its locale alias tag beside it (spec R16a).

    The tagging half of `alias_for`'s readability rule, and the ONE answer the
    writers (`mlo.autotag._alias_tag_values`, the importer's own stamp), script
    8's prescan and the grade all ask — so what a page shows and what a file
    holds can never disagree (R87). A name is annotated only when a reader of
    the configured locale cannot read the script it is written in:

      * a LATIN name never needs one, whatever the locale — "Radiohead" is
        already readable, and a Latin reading of a Latin name is nothing,
      * a name in another script needs one exactly when `locale` does not read
        that script: `宇多田ヒカル` needs "Hikaru Utada" for `en` and needs
        NOTHING for `ja`, where the reader reads the name already.

    The script reading is `mlo.lyrics_xlit`'s (non_latin_ratio /
    dominant_script — the same test that decides whether lyrics need a
    transliteration), and "does the locale read it" is `_reads_natively`, the
    table `alias_for` applies to the alias it picks. A name MusicBrainz states
    no alias for is the writer's other half: nothing is written for nothing.
    """
    text = str(name or "").strip()
    if not text:
        return False
    if (non_latin_ratio(text) < _LATIN_THRESHOLD
            or dominant_script(text) == "latin"):
        # Latin script, or nearly all of it (a stray CJK character in a Latin
        # title is not a name a reader cannot find).
        return False
    return not _reads_natively(text, alias_locale(cfg))


# The three entities a lyrics query is built from, and what MusicBrainz needs
# to answer for each: the path the lookup and the search both live under, the
# key the search results sit in, the field that index calls the name, and the
# key a result states its OWN name under — an artist search answers `name`,
# while recordings and release-groups answer `title` (verified live).
# `inc=aliases` is the whole point of the lookups below.
_ALIAS_ENTITIES = {
    "artist": ("artist", "artists", "artist", "name"),
    "recording": ("recording", "recordings", "recording", "title"),
    "release-group": ("release-group", "release-groups", "releasegroup",
                      "title"),
}

# How many alternative names one lookup may hand back. The lyrics chain walks
# every provider per name, so this is a cost bound, not a completeness one: the
# reader's own locale and a Latin reading are first by construction.
_ALIAS_LIMIT = 6


def _alias_key(value):
    """Comparison key for "is this the same name?": case and spacing ignored.

    MusicBrainz states the same name with its own spacing ("Hikaru  Utada",
    "宇多田 ヒカル"), and a query that only repeats the stored name is a wasted
    provider request — never a different search.
    """
    return " ".join(str(value or "").split()).casefold()


def _alias_by_name(path, results_key, field, name_key, name):
    """The MBID MusicBrainz calls *name* by, or "" — exact and unambiguous.

    A fuzzy result would hand the lyrics chain another song's aliases to search
    with, and so would a same-named DIFFERENT entity (MusicBrainz holds many
    recordings called `光`). Only a result whose own name IS *name* (case and
    spacing ignored) counts, and only when it is the ONLY such result — two
    entities with the name mean there is no honest single answer, and no alias
    pass beats a guessed one.
    """
    if not name:
        return ""
    try:
        data = mb_get_cached(path, {"query": f'{field}:"{_mb_query(name)}"',
                                    "limit": 10, "fmt": "json"})
    except Exception:
        return ""
    key = _alias_key(name)
    found = {str(r.get("id") or "").strip()
             for r in ((data or {}).get(results_key) or [])
             if isinstance(r, dict) and _alias_key(r.get(name_key)) == key}
    found.discard("")
    return found.pop() if len(found) == 1 else ""


def _mb_alias_rows(entity, mbid, name=""):
    """MusicBrainz's alias rows for one entity — [] when it cannot answer.

    An MBID is the honest way to ask: `inc=aliases` on the entity path answers
    with exactly that entity's aliases. Without one the name is searched first
    (the import does not always write MusicBrainz tags) and the entity called
    by that exact name supplies them. Never raises: MusicBrainz being busy or
    unreachable leaves the caller with no aliases, never with an error.
    """
    table = _ALIAS_ENTITIES.get(str(entity or "").strip().lower())
    if table is None:
        return []
    path, results_key, field, name_key = table
    if not mbid:
        mbid = _alias_by_name(path, results_key, field, name_key, name)
        if not mbid:
            return []
    try:
        data = mb_get_cached(f"{path}/{mbid}", {"inc": "aliases", "fmt": "json"})
    except Exception:
        return []
    return [a for a in ((data or {}).get("aliases") or []) if isinstance(a, dict)]


def search_aliases(entity, mbid, cfg=None, name="", limit=_ALIAS_LIMIT):
    """Alternative NAMES to search a lyrics source with, best first.

    The lyrics chain's second pass reads this: the names a source may know a
    track by when the stored one finds nothing — `宇多田ヒカル` is `Hikaru
    Utada`, `光` is `Hikari`, and the reverse for a file tagged the other way
    round. *entity* is "artist", "recording" or "release-group"; *mbid* is that
    entity's MusicBrainz id when the file carries one (an exact-name search
    supplies it otherwise), and *name* is the stored name.

    Ordering is the reader's own preference first — the SAME ladder the pages
    and their parentheses use (`_alias_ladder`: the `locale` setting's name,
    then a Latin reading) — and then whatever else MusicBrainz states, aliases
    in the other script first, which is the reverse direction (`光` for a
    file called "Hikari"). `search hint` aliases are search-index spellings and
    are never query names (`_ALIAS_SKIP_TYPES`), and a name equal to the stored
    one (case and spacing ignored) is dropped.

    Returns a list, [] when MusicBrainz has nothing for the entity or cannot be
    reached. Never raises, never writes, and it costs nothing at all when the
    entity is unknown or neither *mbid* nor *name* is given.
    """
    rows = []
    for row in _mb_alias_rows(entity, mbid, name):
        if str(row.get("type") or "").strip().lower() in _ALIAS_SKIP_TYPES:
            continue
        if str(row.get("name") or "").strip():
            rows.append(row)
    if not rows:
        return []
    want = _locale_preference(cfg)
    seen = {_alias_key(name)}
    out = []

    def add(value):
        value = " ".join(str(value or "").split())
        key = _alias_key(value)
        if not value or key in seen:
            return
        seen.add(key)
        out.append(value)

    # 1. what the reader wants names in — the ladder's own answer, so a page
    #    and the search that follows it call the track the same thing.
    add(_alias_ladder(rows, want, name))
    # 2. a Latin reading of a name that has none (`ロストアンブレラ` ->
    #    "Lost Umbrella"); the ladder's tail, asked on its own so a locale that
    #    did answer does not hide it.
    add(_alias_ladder(rows, "", name))
    # 3. everything else MusicBrainz states, the OTHER script first: a Latin
    #    name whose aliases are Japanese is the reverse of the case above and
    #    is exactly what this list exists for.
    latin_name = _has_latin(name)
    tail = [a for a in rows if _has_latin(a.get("name")) != latin_name]
    tail += [a for a in rows if _has_latin(a.get("name")) == latin_name]
    for row in tail:
        if len(out) >= limit:
            break
        add(row.get("name"))
    return out[:limit]


def _browse_collect(endpoint, extra_params, list_key, count_key, limit=300, offset=0):
    """Browse rows across MusicBrainz's 100-per-request pages.

    Browse has NO server-side sort, so a single arbitrary 100-row slice
    misrepresents a discography (one page can be all albums, the next all
    singles) and any date ordering would be a lie. This walks the pages
    (still 1 req/s) up to `limit` rows starting at `offset` so the caller
    can sort and filter over an honest window. Returns (rows, total, served)
    — `served` being the raw rows MusicBrainz handed back for this window,
    which is what the next offset must follow.

    Rows are de-duplicated by MBID (a browse page that repeats an entity
    must not list it twice); paging stays MusicBrainz's own, which is why
    the raw batch length advances `pos`, not the de-duplicated one."""
    items = []
    seen = set()
    pos = offset
    total = None
    while pos < offset + limit:
        data = mb_get_cached(
            endpoint,
            {**extra_params, "limit": min(100, offset + limit - pos), "offset": pos, "fmt": "json"},
        )
        batch = [r for r in (data.get(list_key) or []) if r.get("id")]
        total = data.get(count_key) or total
        for row in batch:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            items.append(row)
        pos += len(batch)
        if not batch or pos >= min(total or 0, offset + limit):
            break
    if total is None:
        total = len(items)
    # `pos - offset` is how many RAW rows MusicBrainz served for this window;
    # the caller pages by that, so a de-duplicated row can never make the
    # next request overlap the one before it.
    return items, total, pos - offset


def _mbid(value):
    """Extract a MusicBrainz ID from an ID or a musicbrainz.org URL."""
    if not value:
        return None
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value, re.I)
    return m.group(0).lower() if m else None


def _genres(node):
    """MusicBrainz's own genre names for one entity, in its own order.

    The spelling is left exactly as MusicBrainz publishes it (lowercase): the
    writers canonicalize through `mlo.genres.normalize_genres`, and a second
    casing rule here is what used to make the same genre land under two names
    ("Shoegaze" from the tagger, "shoegaze" from the vocabulary).
    """
    return [g["name"] for g in (node.get("genres") or [])]


# --------------------------------------------------------------------------- #
# Release lookups
# --------------------------------------------------------------------------- #
def release_countries(node, preferred="", release_id=""):
    """Every country a release was released in, with its date.

    MusicBrainz keeps a release's events ON the release entity — no `inc` asks
    for them — as one {area, date} per country the pressing appeared in, while
    the entity's own singular `country`/`date` are only the FIRST of them. A
    release group is released in as many countries as its editions cover, so
    the pages show the whole list rather than that first event.

    Returns [{country, code, date, preferred}] ordered by date, then name:
    `country` is MusicBrainz's own area name, `code` its ISO 3166-1 code ("" for
    an area that carries none, which happens for a historic area), `date` the
    event's date, and `preferred` whether the code is the configured
    `prefer_release_country`. An event without an area carries no country at
    all and is dropped — missing data is absent, never invented. `release_id`
    stamps the release that carries the event, for a list that is a release
    group's union over its editions.
    """
    preferred = str(preferred or "").strip().upper()
    out, seen = [], set()
    for event in (node.get("release-events") or []):
        area = event.get("area") or {}
        name = str(area.get("name") or "").strip()
        codes = [str(c).strip().upper() for c in (area.get("iso-3166-1-codes") or []) if str(c).strip()]
        code = codes[0] if codes else ""
        if not name and not code:
            continue                    # a placeless event states no country
        key = (code or name.lower(), str(event.get("date") or ""))
        if key in seen:
            continue
        seen.add(key)
        entry = {
            "country": name or code,
            "code": code,
            "date": str(event.get("date") or ""),
            "preferred": bool(preferred) and code == preferred,
        }
        if release_id:
            entry["release_id"] = release_id
        out.append(entry)
    out.sort(key=lambda e: (e["date"] or "9999", e["country"]))
    return out


def release_group_countries(releases, preferred=""):
    """The union of a release group's editions' events, newest editions
    included.

    `releases` are raw browse rows (they carry `release-events` themselves), in
    the order the caller ranks them, so an event shared by several editions is
    credited to the first-ranked edition that carries it — the same edition
    "Add to library" would take. The union covers the editions of the pages
    loaded so far; a group with more editions than one page is paged, and its
    countries grow as its editions do.
    """
    out, seen = [], set()
    for r in releases or []:
        for event in release_countries(r, preferred, str(r.get("id") or "")):
            key = (event["code"] or event["country"], event["date"])
            if key in seen:
                continue
            seen.add(key)
            out.append(event)
    out.sort(key=lambda e: (e["date"] or "9999", e["country"]))
    return out


def preferred_release_country(cfg=None):
    """`prefer_release_country` — the country the release/release-group country
    chips mark as the user's preference ("" when they set none). Read through
    the same ONE config load the release-choice policy uses, so the marking and
    the policy can never disagree about what the user prefers."""
    return str(_release_cfg(cfg).get("prefer_release_country") or "")


def release_lookup(mbid):
    """Full release: media/discs, recordings, artist credits, genres and
    labels (catalog numbers). Country comes from the release entity.

    Through `mb_get_cached`: the release page, the import wizard's release
    picker and the match step all read this same payload, and at 1 req/s a
    second view of the same release must not cost a second request (the
    cache is what MB's etiquette asks for — this call used to bypass it)."""
    data = mb_get_cached(
        f"release/{mbid}",
        {"inc": "artists+recordings+media+release-groups+artist-credits+genres"
                "+labels+isrcs+aliases", "fmt": "json"},
    )
    # Normalize media into a flat list of {disc, position, title, length, recording mbid, artist mbids}
    tracks = []
    for medium in data.get("media", []):
        disc = medium.get("position", 1)
        for trk in medium.get("tracks", []):
            rec = trk.get("recording", {})
            artists = []
            for ac in trk.get("artist-credit", []):
                if "artist" in ac:
                    artists.append({
                        "name": ac.get("name", ""),
                        "mbid": ac["artist"].get("id"),
                        # MusicBrainz's aliases for this credited artist —
                        # `inc=aliases` above nests them in the same response
                        # (mlo.autotag writes the ARTISTALIAS tags from here) —
                        # and its DISAMBIGUATION comment ("UK rock band"), the
                        # plain-text counterpart the ARTISTDISAMBIGUATION tag
                        # carries. Both ride the same artist row, so neither
                        # costs a request.
                        "aliases": list(ac["artist"].get("aliases") or []),
                        "disambiguation": str(ac["artist"].get("disambiguation")
                                              or "").strip(),
                    })
            tracks.append({
                "position": trk.get("position"),
                "disc": disc,
                "title": trk.get("title"),
                "length": trk.get("length"),
                "recording_mbid": rec.get("id"),
                # This recording's own other-language titles, from the same
                # request (the TITLEALIAS tag's source), and the name of the
                # artist it credits — the ladder needs the name beside the
                # aliases to tell a translation from a repeat of it.
                "aliases": list(rec.get("aliases") or []),
                # This recording's own disambiguation comment when MusicBrainz
                # states one — the name in parentheses after the title a
                # listener sees (mlo.autotag's TITLEDISAMBIGUATION tag).
                "disambiguation": str(rec.get("disambiguation") or "").strip(),
                "artist_name": str(((trk.get("artist-credit") or [{}])[0]
                                    .get("artist") or {}).get("name") or ""),
                "artist_aliases": list(((trk.get("artist-credit") or [{}])[0]
                                        .get("artist") or {}).get("aliases") or []),
                # Whether MusicBrainz states this recording IS a video: with
                # the medium (media[].format) it is what tells a music-video
                # release from an album.
                "video": bool(rec.get("video")),
                "artist_mbids": [a["mbid"] for a in artists],
                "artist_credit": "".join(
                    (ac.get("name", "") + (ac.get("joinphrase", "") or ""))
                    for ac in trk.get("artist-credit", [])
                ),
                "genres": _genre_names(_genres(rec)),
                # ISRCs come free with this request (inc=isrcs) and are what
                # the advisory fetch looks a track up by.
                "isrcs": [v for v in (_isrc(i) for i in (rec.get("isrcs") or [])) if v],
            })
    release_artists = [
        {"name": ac.get("name", ""), "mbid": ac["artist"].get("id"),
         "aliases": list(ac["artist"].get("aliases") or []),
         "disambiguation": str(ac["artist"].get("disambiguation") or "").strip()}
        for ac in data.get("artist-credit", []) if "artist" in ac
    ]
    rg_obj = data.get("release-group") or {}
    primary = (rg_obj.get("primary-type") or "").lower()
    secondary = [s.lower() for s in (rg_obj.get("secondary-types") or [])]
    release_type = "+".join([primary] + secondary) if primary else ""

    # A podcast in MusicBrainz is a SERIES of type Podcast that the release
    # GROUP is `part of` — the relation lives on the group, so it is not in
    # this release payload, and it costs one more (cached) request to ask for.
    # Asked ONLY for the release groups MusicBrainz types Broadcast — which is
    # what every podcast episode it types is (confirmed on 124 episodes of one
    # series: primary-type "Broadcast", some with secondary "Live") — so a
    # music library pays nothing for this feature and a radio-broadcast
    # library pays one request per such release. A refusal is not "not a
    # podcast": the payload carries None and the caller's own tagging run
    # simply writes no podcast tags.
    podcast = None
    if primary and primary == PODCAST_EPISODE_PRIMARY_TYPE.lower():
        try:
            podcast = release_group_series(rg_obj.get("id"))
        except Exception:  # noqa: BLE001 — an MB outage must not lose the release
            podcast = None

    # labels -> label name + every catalog number. A release can carry several
    # (one per label/pressing) and auto-import searches each as its OWN query,
    # so keeping only the first lost every other pressing's number. Order is
    # MusicBrainz's own; blanks and duplicates dropped. `catalog_number` stays
    # the first one for the callers that only ever wanted one.
    label_name = ""
    catalog_numbers = []
    for lab in data.get("label-info", []) or []:
        if not label_name:
            label_name = str(((lab.get("label") or {}).get("name")) or "").strip()
        cn = (lab.get("catalog-number") or "").strip()
        if cn and cn not in catalog_numbers:
            catalog_numbers.append(cn)
    catalog_number = catalog_numbers[0] if catalog_numbers else ""
    country = data.get("country") or ""
    # Every country this pressing was released in (the singular `country` above
    # is MusicBrainz's first event, not the whole story — a worldwide digital
    # reissue of a CD carries both).
    countries = release_countries(data, preferred_release_country())

    return {
        "id": data.get("id"),
        "title": data.get("title"),
        # The title in the reader's locale, when MusicBrainz states one (see
        # `alias_for`) — the page shows it in parentheses beside the title.
        "alias": alias_for(data.get("aliases"), None, data.get("title")),
        # EVERY alias MusicBrainz states, per entity: the release's own, its
        # group's and the credited artist's (plus each recording's, in
        # `media`). `mlo.autotag` turns them into the ALBUMALIAS /
        # ARTISTALIAS / TITLEALIAS tags an import stamps on the files;
        # `inc=aliases` above already carried them, so they cost no request.
        "aliases": list(data.get("aliases") or []),
        "release_group_aliases": list((data.get("release-group") or {})
                                      .get("aliases") or []),
        "artist_aliases": list((release_artists[0].get("aliases")
                                if release_artists else []) or []),
        # MusicBrainz's own pressing comment ("Deluxe Edition", "2011
        # remaster") — empty when it states none, which is what every reader
        # treats as "no disambiguation".
        "disambiguation": data.get("disambiguation") or "",
        # The same KIND of comment one level up and beside the name: the
        # release GROUP's comment ("The Blue Album" for 1967–1970) and the
        # credited artist's ("UK rock band"). `inc=release-groups` and
        # `artist-credit` above already carried both, so neither costs a
        # request; the album-level half of mlo.autotag reads them into the
        # ALBUMDISAMBIGUATION / ARTISTDISAMBIGUATION tags, and the album pages
        # render them in parentheses after the album and artist names.
        "release_group_disambiguation": str(rg_obj.get("disambiguation")
                                            or "").strip(),
        "artist_disambiguation": str((release_artists[0].get("disambiguation")
                                      if release_artists else "") or "").strip(),
        "date": (data.get("date") or ""),
        # the release-group's first-release-date — the "original" release
        # date shown next to this specific release's own date
        "originaldate": (rg_obj.get("first-release-date") or ""),
        "barcode": (data.get("barcode") or ""),
        "country": country,
        # every country of this pressing, with its date and the preference mark
        "countries": countries,
        # Status (Official/Promotion/Bootleg/…) + the medium of the first
        # medium — the release page meta line shows both, and the auto-import
        # policy keys on them.
        "status": data.get("status") or "",
        "medium": next((m.get("format") or "" for m in data.get("media", [])), ""),
        "catalog_number": catalog_number,
        # All of them, in MusicBrainz order — the auto-import search expands
        # this into one query per number.
        "catalog_numbers": catalog_numbers,
        "label": label_name,
        "release_group_id": (data.get("release-group") or {}).get("id"),
        # `release_type` keeps its historical lowercase "+"-joined spelling —
        # it is written into the RELEASETYPE tag and drives the naming script
        # and grading. The structured pair below is for display: MusicBrainz
        # splits release types into primary (Album/EP/Single/Broadcast/Other)
        # and secondary (Soundtrack/Live/Compilation/Remix/Demo/...).
        "release_type": release_type,
        "primary_type": rg_obj.get("primary-type") or "",
        "secondary_types": [s for s in (rg_obj.get("secondary-types") or [])],
        # The Podcast SERIES this release's group is `part of`, or None (see
        # the block above: read only for a Broadcast group, which is the type
        # MusicBrainz gives a podcast episode). `mlo.autotag.mb_track_tags`
        # turns it into the PODCASTSERIES / PODCASTSERIESMBID /
        # PODCASTEPISODE tags every writer of this payload shares.
        "podcast": podcast,
        "artists": release_artists,
        "genres": _display_genres(_genre_names(_genres(data))),
        "media": tracks,
        "medium_count": len(data.get("media", [])),
        "medium_formats": [m.get("format") or "" for m in data.get("media", [])],
        # The release's TEXT REPRESENTATION — MusicBrainz's own statement of
        # the language its lyrics are in and the script they are written in
        # (``{"language": "jpn", "script": "Jpan"}``, ISO 639-3 + ISO 15924;
        # it comes with the release lookup itself, so this costs no request).
        # THIS is what an import stamps into the tracks' LANGUAGE tag, and what
        # script 17's transliteration/translation decision reads (spec R167) —
        # the one thing a Latin-script text cannot state about itself. `mul`
        # ("several languages", a compilation) is passed through as it is:
        # `mlo.lyrics_xlit.normalize_lang` is what knows it says nothing.
        "language": str((data.get("text-representation") or {}).get("language") or ""),
        "script": str((data.get("text-representation") or {}).get("script") or ""),
    }


# --------------------------------------------------------------------------- #
# Podcasts: a SERIES of type "Podcast", never a release-group type
# --------------------------------------------------------------------------- #
# MusicBrainz has no "Podcast" release-group type (the primary types are
# Album/EP/Single/Broadcast/Other). What it has is a SERIES of type Podcast
# (/ws/2/series/<id> → "type":"Podcast", type-id below, ~1.35k of them), and
# an episode is a release group linked to that series with a `part of`
# relationship — confirmed live: `/ws/2/release-group/<mbid>?inc=series-rels`
# answers
#   {"relations": [{"type": "part of", "target-type": "series",
#                   "attribute-values": {"number": "2515"},
#                   "series": {"type": "Podcast", "name": "New Sounds",
#                              "disambiguation": "WNYC radio show", ...}}]}
# and the relationship lives on the RELEASE GROUP, never on the release (a
# release's own `inc=series-rels` is empty). The series relations ride along
# on a release-group BROWSE too (`?artist=<mbid>&inc=series-rels`), which is
# what makes the app's discography paths carry the identity at no extra
# request.
PODCAST_SERIES_TYPE = "Podcast"
PODCAST_SERIES_TYPE_ID = "ef6f7b93-868a-43a9-b59c-a73b62c2c51e"
# The release-group PRIMARY type MusicBrainz gives a podcast episode. It is
# what decides whether a release lookup is worth one more request for the
# series relation (see `release_lookup`): nothing else in a music library is
# a Broadcast, and every episode of the series checked is one.
PODCAST_EPISODE_PRIMARY_TYPE = "Broadcast"


def podcast_series_of(node):
    """The Podcast SERIES *node* is `part of`, or None.

    *node* is a release-group payload that asked for `inc=series-rels` (see
    `release_group_series` for the lookup, `artist_release_groups` and
    `release_group_browse` for the browse paths). A release group can be
    `part of` SEVERAL series — a radio show's episodes are often parts of both
    the podcast series and the program's own series — so the relations are
    filtered twice: to `part of` relations pointing at a SERIES, and then to
    the one that IS a Podcast (by type-id first — a renamed or translated type
    label cannot make the app stop seeing podcasts — falling back to MusicBrainz's
    own type name). Returns
    ``{"mbid", "name", "disambiguation", "title", "number"}`` where `title` is
    the name a reader sees (with MusicBrainz's disambiguation in parentheses
    when it states one, so two same-named series stay apart) and `number` is
    the episode number MusicBrainz states on the relationship ("" when it
    states none).

    None when the group is in no Podcast series — including the ordinary case
    of a release group with no series relations at all.
    """
    node = node if isinstance(node, dict) else {}
    found = []
    for rel in node.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        if str(rel.get("type") or "").strip().lower() != "part of":
            continue
        series = rel.get("series")
        if not isinstance(series, dict):
            continue
        if str(rel.get("target-type") or "series") != "series":
            continue
        if (str(series.get("type-id") or "").strip().lower() != PODCAST_SERIES_TYPE_ID
                and str(series.get("type") or "").strip().lower()
                != PODCAST_SERIES_TYPE.lower()):
            continue
        attrs = rel.get("attribute-values")
        number = ""
        if isinstance(attrs, dict):
            number = str(attrs.get("number") or "").strip()
        name = str(series.get("name") or "").strip()
        disambiguation = str(series.get("disambiguation") or "").strip()
        found.append({
            "mbid": series.get("id") or "",
            "name": name,
            "disambiguation": disambiguation,
            "title": f"{name} ({disambiguation})" if disambiguation else name,
            "number": number,
        })
    if not found:
        return None
    # Several Podcast series on one release group (a re-published episode):
    # the one MusicBrainz states an episode number for is the episode's own,
    # and otherwise MusicBrainz's relation order decides — never a random one.
    return next((s for s in found if s["number"]), found[0])


def release_group_series(rg_mbid):
    """The Podcast series the release group *rg_mbid* is `part of`, or None.

    ONE cached MusicBrainz request (`release-group/<id>?inc=series-rels`): the
    relation is not part of a release payload, so a path that starts from a
    RELEASE has to ask the group — see `mlo.autotag`, which asks only for the
    releases MusicBrainz types Broadcast rather than for every album in a
    library. An outage is not "not a podcast": this raises MusicBrainzError
    (mb_get_cached's own refusal) and the caller decides what to do with it.
    """
    mbid = _mbid(rg_mbid)
    if not mbid:
        return None
    return podcast_series_of(mb_get_cached(f"release-group/{mbid}",
                                           {"inc": "series-rels", "fmt": "json"}))


def recording_isrcs(recording_mbid):
    """ISRCs MusicBrainz holds for a recording (usually exactly one)."""
    if not recording_mbid:
        return []
    try:
        data = mb_get_cached(f"recording/{recording_mbid}",
                             {"inc": "isrcs", "fmt": "json"})
    except Exception:
        return []
    return [v for v in (_isrc(i) for i in (data.get("isrcs") or [])) if v]


# --------------------------------------------------------------------------- #
# Content advisory (ITUNESADVISORY): 0 = not explicit, 1 = explicit, 2 = safe.
# --------------------------------------------------------------------------- #
# EVERY applicable source is asked for EVERY track — one pass, no short
# circuit — and the answers are collected per source and MERGED by a single
# documented rule (`merge_advisory`), so a source that states "not explicit"
# is heard even after another one already said "explicit", and the caller can
# show what each source said:
#
#   deezer-isrc    api.deezer.com/track/isrc:<ISRC>      (verified working)
#   spotify-isrc   Spotify search by ISRC                (needs client id+secret)
#   apple-album    iTunes artist → its albums → THE EXPLICIT EDITION →
#                  lookup?entity=song                    (verified working)
#   itunes-song    iTunes song search, exact title only  (last resort)
#   youtube-age    yt-dlp `age_limit` >= 18 for the track's OWN video id
#                  (explicit-only, last, never a blind per-track search)
#
# NOT a route any more: Discogs' edition `format`/`description`
# "Parental Advisory". It is an ALBUM/EDITION-level flag, so it could not
# state anything about ONE track — merged in as an explicit `1` it made every
# track of a flagged edition read explicit (the owner's "tagging tracks as
# explicit even when they aren't"). An explicit flag now needs a source that
# states the TRACK is explicit (an ISRC route, or Apple's own
# `trackExplicitness`). The Discogs reader it used
# (`discovery.discogs_parental_advisory`) is gone with it.
#
# ISRC path: the two ISRC sources are asked for every ISRC the track has —
# the file's own tag, or every ISRC MusicBrainz holds for its recording.
# APPLE HAS NO ISRC LOOKUP, which is why it is reached by artist → album
# edition instead; it is still cross-referenced with both ISRC sources.
#
# VERIFIED, on this machine: Apple's own `lookup?isrc=` and `lookup?upc=`
# endpoints answer resultCount 0 — Apple does not serve identity lookups, so
# Apple is reached through the artist's album list (or the exact-title song
# search). APPLE'S ALBUM *SEARCH* IS NOT USABLE FOR THIS: `search?entity=album`
# answers ONE edition — for System Of A Down's "Steal This Album!" the CLEANED
# one — whose tracks then read `cleaned`/`notExplicit` over the whole album,
# i.e. an explicitly clean re-release of an explicit master. The artist route
# (`lookup?id=<artistId>&entity=album`) lists every edition with its own
# `collectionExplicitness`, so the explicit edition is the one whose tracks are
# read, and the cleaned edition is only a cross-check.
#
# Deezer's ISRC endpoint is per-track and needs no title guessing at all.
# Spotify is optional and never load-bearing; it is skipped entirely until
# `spotify_client_id`/`spotify_client_secret` are set in Settings.
#
# Honesty rules, which every route below obeys:
#   * a route only ever returns an answer it can attribute to itself, and an
#     answer it cannot state is NO answer (never a guess, never a default);
#   * a `cleaned` Apple entry (or a `Clean` contentAdvisoryRating) is a CLEANED
#     EDITION, states nothing about the original master and is NO ANSWER — it
#     must never become 0 or 2;
#   * a name-based match never accepts a variant of the track, and a variant
#     never accepts the original (`title_matches`);
#   * the merged value is 0 when nobody stated anything at all: an unstated
#     advisory is written as "not explicit", the user's policy, and the
#     `answers` map is what says whether any source actually spoke.
# Every value a route may report as `source`. A caller may only write a value
# it can attribute to one of these.
ADVISORY_SOURCES = frozenset({"deezer-isrc", "spotify-isrc", "apple-album",
                              "itunes-song", "youtube-age"})

_ITUNES_LOOKUP = "https://itunes.apple.com"
_DEEZER_TRACK_ISRC = "https://api.deezer.com/track/isrc:"
_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SPOTIFY_SEARCH = "https://api.spotify.com/v1/search"
# Album titles are compared with difflib below this ratio = not this release.
_APPLE_ALBUM_SIMILARITY = 0.6
# iTunes rate-limits hard: hammering it earns an EMPTY body (not JSON), and a
# library pass re-reads the same artist and the same album's tracks over and
# over. Calls are spaced out AND cached — in memory by the transport, on disk
# below so a restart does not re-ask what Apple already told us.
_APPLE_MIN_INTERVAL = 3.0
_APPLE_CACHE_TTL = 30 * 86400.0
_APPLE_CACHE_NAME = "apple_cache"
_apple_lock = threading.Lock()
_apple_last = 0.0

# One answer per (source, identity). A library pass asks each track once;
# `None` (asked, nobody stated a value) is cached too, so a rerun that found
# nothing does not re-hit the APIs either. Bounded — it is a memo, not a store.
_ADVISORY_CACHE: dict = {}
_ADVISORY_LOCK = threading.Lock()
_ADVISORY_MISS = object()
_SPOTIFY_TOKEN: dict = {}
# Why Spotify last refused the client credentials: `{reason, at}`. The token
# cache only ever holds a success, so without this a rejected client id/secret
# left the sources silently skipped — see `spotify_last_error`.
_SPOTIFY_LAST: dict = {}
_ADVISORY_CACHE_MAX = 20000


def _advisory_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _advisory_json(url, params=None, headers=None, timeout=None, host=None):
    """The one HTTP seam every advisory route goes through: discovery's
    per-host-throttled, TTL-cached JSON client, so a library pass is polite
    and a repeat ask costs no request. None on any failure."""
    from server import discovery
    return discovery._json(url, params, headers=headers, timeout=timeout, host=host)


def _advisory_post(url, data=None, headers=None, timeout=None):
    """POST sibling of `_advisory_json` (only Spotify's token endpoint needs
    one). -> `(body, error)`, where `error` is the endpoint's own refusal in
    its own words ("" when it answered).

    The body is returned even for a refusal: OAuth2 states a rejected client
    IN the 400 body (`{"error": "invalid_client", "error_description": ...}`),
    and a caller that could only see None could not tell a wrong client secret
    from an unreachable host — which is exactly the difference a Test button
    has to report."""
    try:
        r = httpx.post(url, data=data, headers=headers, timeout=timeout or 15.0)
        if r.status_code >= 400:
            return None, f"HTTP {r.status_code}: {(r.text or '').strip()[:300]}"
        return r.json(), ""
    except Exception as e:
        return None, f"no answer ({type(e).__name__}: {e})"


def _advisory_cached(key, producer):
    """Memoized `producer()` — answer or None, cached either way."""
    with _ADVISORY_LOCK:
        hit = _ADVISORY_CACHE.get(key)
    if hit is not None:
        return None if hit is _ADVISORY_MISS else hit
    answer = producer()
    with _ADVISORY_LOCK:
        if len(_ADVISORY_CACHE) >= _ADVISORY_CACHE_MAX:
            _ADVISORY_CACHE.clear()
        _ADVISORY_CACHE[key] = _ADVISORY_MISS if answer is None else answer
    return answer


def _deezer_advisory(isrc, timeout=None):
    """(value, source) for one track from Deezer's ISRC lookup, or None.

    Deezer states two things and both are read, but the TRACK's own lyrics
    flag is decisive:
      * `explicit_lyrics: false` — Deezer stating the track's LYRICS are not
        explicit — is 0, whatever `explicit_content_lyrics` says. The two
        fields are separate, and a record with `explicit_lyrics: false` and a
        content flag of 2 ("explicit content", an edition/artwork flag, the
        value the owner's "Just (Edit)" pressing carries) is NOT a statement
        that the track is explicit. Reading the content flag first made the
        app mark a clean track explicit off its own edit's record — and it
        contradicted this function's own rule.
      * `explicit_content_lyrics` 1 and 2 are explicit statements — the same
        "explicit content" an explicit flag carries — but only when
        `explicit_lyrics` is ABSENT. Mapping an explicit flag to 2 would
        relabel an explicit track as safe: this app's 2 means "clean edition",
        not "explicit".
      * `explicit_content_lyrics: 0` → 0, the same clean statement.
      * anything else (the unclassified value Deezer reports as 3) is NO
        ANSWER, and never 0.
    """
    code = str(isrc or "").strip()
    if not code:
        return None
    data = _advisory_json(_DEEZER_TRACK_ISRC + code, timeout=timeout,
                          host="api.deezer.com")
    if not isinstance(data, dict) or data.get("error"):
        return None
    content = _advisory_int(data.get("explicit_content_lyrics"))
    lyrics = data.get("explicit_lyrics")
    # The TRACK's own lyrics flag decides when Deezer states it: `true` is 1,
    # `false` is 0 — whatever the content flag says. `explicit_content_lyrics`
    # is only consulted when `explicit_lyrics` is absent, and its 1/2 are the
    # explicit statements (`0` is the clean one, 3 unclassified → no answer).
    if lyrics is True:
        return (1, "deezer-isrc")
    if lyrics is False:
        return (0, "deezer-isrc")
    if content in (1, 2):
        return (1, "deezer-isrc")
    if content == 0:
        return (0, "deezer-isrc")
    return None


def _spotify_token_post(cid, secret, timeout=None):
    """One `POST /api/token` with the client-credentials grant.

    -> `(body, error)`; the ONE place the Basic header is built, so the token
    the sources use and the token a Test button asks for cannot drift apart.
    """
    import base64
    return _advisory_post(
        _SPOTIFY_TOKEN_URL,
        data={"grant_type": "client_credentials"},
        headers={"Authorization": "Basic " + base64.b64encode(
            f"{cid}:{secret}".encode("utf-8")).decode("ascii"),
            "Content-Type": "application/x-www-form-urlencoded"},
        timeout=timeout)


def _spotify_token(cfg, timeout=None):
    """Client-credentials token, or None when Spotify is unconfigured/down.

    `spotify_client_id` + `spotify_client_secret` are optional; without them
    this returns None and the route is skipped, never defaulted. A REFUSED
    client keeps its refusal in `_SPOTIFY_LAST` so the caller can report
    Spotify's own words instead of an empty skip — see `spotify_last_error`.
    """
    cid = str((cfg or {}).get("spotify_client_id") or "").strip()
    secret = str((cfg or {}).get("spotify_client_secret") or "").strip()
    if not cid or not secret:
        return None
    with _ADVISORY_LOCK:
        token = _SPOTIFY_TOKEN.get("token")
        if token and _SPOTIFY_TOKEN.get("expires", 0) > time.time():
            return token
    body, error = _spotify_token_post(cid, secret, timeout=timeout)
    token = (body or {}).get("access_token")
    if not token:
        _record_spotify_refusal(error or "no access_token in the answer")
        return None
    with _ADVISORY_LOCK:
        _SPOTIFY_TOKEN["token"] = token
        _SPOTIFY_TOKEN["expires"] = (
            time.time() + float((body or {}).get("expires_in") or 3600) - 60)
    return token


def _record_spotify_refusal(reason):
    with _ADVISORY_LOCK:
        _SPOTIFY_LAST.update(reason=str(reason or ""), at=time.time())


def spotify_last_error(since=None):
    """Why Spotify last refused the client credentials, or "" — see
    `spotify_check`. `since` filters out a refusal from an earlier run."""
    with _ADVISORY_LOCK:
        got = dict(_SPOTIFY_LAST)
    if not got or (since and float(got.get("at") or 0) < float(since)):
        return ""
    return str(got.get("reason") or "")


def spotify_check(cfg=None, timeout=None):
    """Are the saved client credentials accepted? -> `{ok, checked, detail}`.

    `POST /api/token` with the client-credentials grant IS the authentication
    the sources then depend on, so this asks the same endpoint they do (through
    the same `_spotify_token_post` seam). A rejected pair answers
    `{"error": "invalid_client", …}` in a 400 body, which is reported
    verbatim. Live by construction: the token cache is bypassed so a Test
    after pasting a new secret asks Spotify again."""
    cid = str((cfg or {}).get("spotify_client_id") or "").strip()
    secret = str((cfg or {}).get("spotify_client_secret") or "").strip()
    if not cid or not secret:
        return {"ok": False, "checked": "",
                "detail": "no spotify_client_id/spotify_client_secret is set"}
    started = time.time()
    body, error = _spotify_token_post(cid, secret, timeout=timeout)
    token = str((body or {}).get("access_token") or "").strip()
    if token:
        return {"ok": True, "checked": "POST /api/token (client_credentials)",
                "detail": "client credentials accepted — POST /api/token "
                          "issued a token"}
    _record_spotify_refusal(error or "no access_token in the answer")
    return {"ok": False, "checked": "POST /api/token (client_credentials)",
            "detail": "Spotify rejected the client credentials — "
                      + (error or spotify_last_error(started))}


def _spotify_advisory(isrc, cfg, timeout=None):
    """(value, source) from Spotify's ISRC search, or None.

    Only a hit whose own `external_ids.isrc` is the ISRC we asked about
    counts — a search hit is a candidate, not an identity.
    """
    code = str(isrc or "").strip()
    token = _spotify_token(cfg, timeout=timeout) if code else None
    if not token:
        return None
    data = _advisory_json(
        _SPOTIFY_SEARCH,
        {"q": f"isrc:{code}", "type": "track", "limit": 1},
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout, host="api.spotify.com")
    for item in (((data or {}).get("tracks") or {}).get("items") or []):
        got = str((item.get("external_ids") or {}).get("isrc") or "").strip()
        if got.upper() != code.upper():
            continue
        return (1 if item.get("explicit") else 0, "spotify-isrc")
    return None


def _data_cache_dir(name):
    """<music>/.mlo/data/<name> — the app's folder-state dir for *name*."""
    from mlo.paths import app_data_dir
    music = ""
    try:
        from mlo.config import load_config
        music = str(load_config().get("music_folder") or "")
    except Exception:
        music = ""
    d = app_data_dir(music or None) or ""
    return os.path.join(d, name) if d else None


def _apple_cache_dir():
    """Disk cache for the artist→albums and album→tracks payloads."""
    return _data_cache_dir(_APPLE_CACHE_NAME)


def _apple_json(path, params, timeout=None):
    """One iTunes GET: ~1 per _APPLE_MIN_INTERVAL, disk-cached for a month.

    Apple rate-limits hard — rapid repeats answer an EMPTY body, not JSON —
    and these payloads (an artist's whole album list, one album's tracks) are
    what a library pass re-reads for every file. None on any failure; this
    never raises.
    """
    global _apple_last
    import hashlib
    from urllib.parse import urlencode
    key = hashlib.sha1((path + "?" + urlencode(
        sorted((str(k), str(v)) for k, v in (params or {}).items())
    )).encode("utf-8")).hexdigest()
    d = _apple_cache_dir()
    fp = os.path.join(d, key + ".json") if d else None
    if fp:
        try:
            if (os.path.isfile(fp)
                    and time.time() - os.path.getmtime(fp) < _APPLE_CACHE_TTL):
                with open(fp, encoding="utf-8") as fh:
                    return json.load(fh)
        except (OSError, ValueError):
            pass
    # The spacing is the lock's whole job, and it is taken for the spacing
    # ONLY: the request happens outside it. Held across the call — the shape
    # this had — one slow answer stalled every other Apple caller behind it,
    # and the interval became a ceiling for the whole client instead of a gap
    # between requests (`discovery._throttle` has always stamped the time, let
    # go and only then asked). Measured on one album's advisory pass with
    # several callers waiting: 38 s of it was spent asleep inside this lock,
    # against 5 s for the same work with the lock held for the spacing only.
    with _apple_lock:
        wait = _APPLE_MIN_INTERVAL - (time.time() - _apple_last)
        if wait > 0:
            time.sleep(wait)
        _apple_last = time.time()
    data = _advisory_json(f"{_ITUNES_LOOKUP}{path}", dict(params or {}),
                          timeout=timeout, host="itunes.apple.com")
    if data is None or not fp:
        return data
    try:
        os.makedirs(d, exist_ok=True)
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh)
        os.replace(tmp, fp)
    except OSError:
        pass
    return data


def _apple_country(cfg=None):
    """The storefront Apple is asked about — the app's region setting."""
    return (str((cfg or {}).get("cover_country") or "").strip().lower()
            or "us")


def _apple_artist_id(artist, timeout=None):
    """Apple's artist id for a name, or None.

    Only the artist route lists EVERY edition of a release, so this is where
    the advisory route starts.
    """
    name = str(artist or "").strip()
    if not name:
        return None
    data = _apple_json("/search", {"term": name, "entity": "musicArtist",
                                   "limit": 5}, timeout=timeout)
    want = _norm_compare(name)
    for row in (data or {}).get("results") or []:
        if _norm_compare(row.get("artistName")) == want:
            return _advisory_int(row.get("artistId"))
    return None


def _apple_editions(artist, album, track_count=None, cfg=None, timeout=None):
    """Apple's editions of one album, the EXPLICIT edition first.

    `lookup?id=<artistId>&entity=album` lists every edition Apple holds, each
    with its own `collectionExplicitness`. An edition counts as this album
    when its normalized `collectionName` matches — a `trackCount` match only
    corroborates (it sorts such an edition up, and lets a near-identical title
    through). Every failure — no artist, no editions, a rate-limited empty
    body — returns [].
    """
    artist_id = _apple_artist_id(artist, timeout=timeout)
    if not artist_id:
        return []
    data = _apple_json("/lookup",
                       {"id": artist_id, "entity": "album", "limit": 200,
                        "country": _apple_country(cfg)}, timeout=timeout)
    want_artist = _norm_compare(artist)
    want_album = _norm_compare(album)
    want_count = _advisory_int(track_count)
    rows = []
    for row in (data or {}).get("results") or []:
        cid = _advisory_int(row.get("collectionId"))
        if not cid:
            continue
        got_artist = _norm_compare(row.get("artistName"))
        if want_artist and got_artist and want_artist not in got_artist:
            continue
        name = _norm_compare(row.get("collectionName"))
        # An instrumental/karaoke edition of the album is not the album: its
        # tracks would rate the wrong recording, so a variant mismatch is out
        # for both the name match and the similarity fallback below.
        kind = title_variant_kind(album)
        got_kind = title_variant_kind(row.get("collectionName"))
        if (kind or got_kind) and kind != got_kind:
            continue
        count_ok = bool(want_count
                        and _advisory_int(row.get("trackCount")) == want_count)
        named = bool(want_album and name == want_album)
        if not (named or (count_ok and _similarity(
                album, str(row.get("collectionName") or ""))
                >= _APPLE_ALBUM_SIMILARITY)):
            continue
        explicit = str(row.get("collectionExplicitness") or "").strip().lower()
        rows.append((explicit == "explicit", count_ok, named, row))
    rows.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    return [row for *_rank, row in rows]


def _apple_value(item):
    """Apple's track payload → 1/0/None.

    `trackExplicitness` is the field that matters. `cleaned` is a CLEANED
    EDITION — an edited master — and `Clean` in `contentAdvisoryRating` marks
    the same edition; neither states anything about the original, so both are
    NO ANSWER. Without that guard the clean re-release of an explicit album
    would quietly relabel every track safe.
    """
    exp = str(item.get("trackExplicitness") or "").strip().lower()
    rating = str(item.get("contentAdvisoryRating") or "").strip().lower()
    if exp == "cleaned" or rating == "clean":
        return None
    if exp == "explicit" or rating == "explicit":
        return 1
    if exp == "notexplicit":
        return 0
    return None


def _apple_collection_value(cid, title="", disc=None, track=None,
                            positions_ok=True, timeout=None, cfg=None):
    """The advisory Apple states for the file inside one collection.

    The file is mapped to its track by discNumber/trackNumber (the position
    decides — another track's rating is not this file's), falling back to a
    title match that refuses variants (`title_matches`): an instrumental or
    karaoke version of the track sits in the same album and is a different
    recording. `positions_ok=False` disables the position mapping entirely:
    that collection does not hold the album's track count, so the same
    disc/track number may be a different song and only a title match may be
    trusted.

    Returns None when the matched track is `cleaned` or carries no rating:
    that collection simply cannot answer, and the CALLER may still ask the
    album's other editions.
    """
    songs = _apple_json("/lookup",
                        {"id": cid, "entity": "song", "limit": 200,
                         "country": _apple_country(cfg)}, timeout=timeout)
    tracks = [item for item in (songs or {}).get("results") or []
              if str(item.get("wrapperType") or "").lower() == "track"]
    want_disc, want_track = _advisory_int(disc), _advisory_int(track)
    if positions_ok and want_disc is not None and want_track is not None:
        for item in tracks:
            if (_advisory_int(item.get("discNumber")) == want_disc
                    and _advisory_int(item.get("trackNumber")) == want_track):
                return _apple_value(item)
    if str(title or "").strip():
        for item in tracks:
            if not title_matches(title, item.get("trackName")):
                continue
            value = _apple_value(item)
            if value is not None:
                return value
            break   # the title matched a `cleaned` entry — that is the answer
    return None


def _apple_album_advisory(artist, album, title="", disc=None, track=None,
                          track_count=None, timeout=None, cfg=None):
    """(value, source) from the album's editions on Apple, or None.

    The long way round, on purpose: artist search → artist id → the artist's
    album list → the EXPLICIT edition first (`_apple_editions`), then the
    cleaned / other editions as a cross-check. Apple's album *search* answers
    one edition only — often the cleaned one, whose tracks then read
    `cleaned`/`notExplicit` even where the master is explicit.

    A `cleaned` match never produces a value — it only proves that THIS
    edition cannot rate the track — so the next candidate is tried (bounded to
    three track lookups) before giving up. Candidates whose track count is
    known and differs are title-matched only, because position alone would
    then be matching a different song.
    """
    artist = str(artist or "").strip()
    album = str(album or "").strip()
    if not artist or not album:
        return None
    want_count = _advisory_int(track_count)
    for row in _apple_editions(artist, album, track_count, cfg,
                               timeout=timeout)[:3]:
        cid = _advisory_int(row.get("collectionId"))
        if not cid:
            continue
        count_ok = bool(want_count
                        and _advisory_int(row.get("trackCount")) == want_count)
        value = _apple_collection_value(
            cid, title, disc, track,
            positions_ok=want_count is None or count_ok,
            timeout=timeout, cfg=cfg)
        if value is not None:
            return (value, "apple-album")
    return None


def _norm_compare(text):
    """Comparison key for titles/artists.

    discovery's own normalizer, deliberately: the app must not grow a second
    opinion about when two titles are the same string.
    """
    from server import discovery
    return discovery.norm(text)


# --------------------------------------------------------------------------- #
# Title variants: an instrumental / karaoke / demo / cover / tribute version
# is NOT the track itself. A per-track value may never be taken from one of
# them (`title_variant_kind`), and a file whose own name says it is a variant
# may never take the original's data. `title_matches` is that guard, and every
# name-based match in this module goes through it.
# --------------------------------------------------------------------------- #
# (kind, pattern) — first hit wins; checked in this order on purpose, so
# "(Karaoke Instrumental Version)" reads instrumental.
_VARIANT_MARKERS = (
    ("instrumental", r"\(\s*instrumental\s*\)"),
    ("instrumental", r"\binstrumental\s+version\b"),
    ("instrumental", r"^\s*instrumental\s*[-–—:]"),
    ("karaoke", r"\bkaraoke\b"),
    ("karaoke", r"\bbacking\s+track\b"),
    ("demo", r"\(\s*demo\s*\)"),
    ("cover", r"\bcover\s+version\b"),
    ("tribute", r"\btribute\b"),
    ("tribute", r"\bmade\s+famous\s+by\b"),
    ("tribute", r"\bin\s+the\s+style\s+of\b"),
)


def title_variant_kind(title):
    """None, or which kind of variant this title says it is.

    "karaoke", "instrumental", "demo", "cover" or "tribute" — the markers are
    the ones releases actually carry: `(Instrumental)`, `Instrumental
    Version`, a leading `Instrumental -`/`Karaoke -`, `Karaoke`, `Backing
    Track`, `(Demo)`, `Cover Version`, `Tribute`, `Made Famous By`, `In The
    Style Of`. Anything else (including a plain title) is None.
    """
    text = str(title or "")
    for kind, pattern in _VARIANT_MARKERS:
        if re.search(pattern, text, re.I):
            return kind
    return None


def title_matches(query_title, candidate_title, *, allow_variant=False):
    """True when `candidate_title` is the same title as `query_title`.

    Normalized comparison (discovery's normalizer), with the variant guard on
    top: a candidate that is an instrumental/karaoke/… version of the query is
    NOT the query, and a variant query does not accept the original either —
    unless `allow_variant`, which ignores the guard and compares names only.
    """
    query = _norm_compare(query_title)
    candidate = _norm_compare(candidate_title)
    if not query or not candidate:
        return False
    if not allow_variant:
        want = title_variant_kind(query_title)
        got = title_variant_kind(candidate_title)
        if (want or got) and want != got:
            return False
    return query == candidate


def merge_advisory(answers, fallback=None):
    """The ONE merge rule for ITUNESADVISORY — `{source: 0|1|2}` → 0|1|2|None.

    The user's policy, in this order:

      1. ANY source states explicit (1) → 1. One source finding it explicit
         settles it, however many others say otherwise.
      2. else ANY source states clean (0) → 0. In the clean family 0 plays the
         same role 1 plays in the explicit family: a source that says the
         track is not explicit outranks a source that only says a clean
         EDITION exists ("most sources say 2 but one says 0" → 0).
      3. else ANY source states a clean edition (2) → 2.
      4. else `fallback` — None by default, which says "nobody stated
         anything" instead of inventing a value. The callers that must write
         something (the import's advisory step) run the rest of the ladder
         first: see `mlo.advisory.decide_advisory`, and `advisory_fallback`
         for the last resort.

    THE FALSE-EXPLICIT GUARD. Step 1 has ONE exception, added because a clean
    track must never be written explicit off a loose match: when an IDENTITY
    route states CLEAN (0) and the only 1 comes from a NAME-guess route, the
    clean wins. An identity route matched the track by its own ISRC
    (`deezer-isrc`, `spotify-isrc`) — the same recording, not a lookalike —
    while `apple-album` (a name+track-number match, whose explicit EDITION is
    preferred when one exists) and `itunes-song` (a title search) are guesses
    the providers themselves get wrong: the owner's "Just" case is exactly a
    track whose loose Apple match said explicit while Deezer's ISRC said
    clean. `youtube-age` is video-level and never clears a track either way.
    Two identity routes that disagree still take the rank above (1 wins), and
    a 1 from an identity route is never overruled by a name-route 0.

    The same rank settles ONE source's several answers before this runs
    (`_strongest_advisory`, which `resolve_advisory_route` applies as each
    answer arrives): a source asked once per pressing states "explicit" for
    the same track its other answer calls clean, and ask order must not
    decide which of the two counts.
    """
    answers = answers or {}
    values = {_advisory_int(v) for v in answers.values()}
    if 1 in values:
        identity_clean = any(_advisory_int(answers.get(s)) == 0
                             for s in _ADVISORY_IDENTITY_SOURCES)
        identity_explicit = any(_advisory_int(answers.get(s)) == 1
                                for s in _ADVISORY_IDENTITY_SOURCES)
        if 0 in values and identity_clean and not identity_explicit:
            return 0
    for value in (1, 0, 2):
        if value in values:
            return value
    return fallback


# The advisory routes that match a track by IDENTITY (its own ISRC) rather
# than by name — see `merge_advisory`'s guard. A name-guess route's explicit
# cannot overrule one of these stating clean.
_ADVISORY_IDENTITY_SOURCES = ("deezer-isrc", "spotify-isrc")


def _strongest_advisory(current, answer):
    """The stronger of one source's two answers — 1 beats 0 beats 2.

    A source can answer more than once for the SAME track: Deezer and Spotify
    are asked once per ISRC the file states, plus every ISRC MusicBrainz holds
    for its recording. A recording whose pressings disagree would otherwise be
    settled by ASK ORDER — the first pressing's clean answer would suppress a
    later pressing's explicit one, and a track Deezer itself flags explicit
    would be written 0. The rank is `merge_advisory`'s own (explicit anywhere
    wins, then a stated clean, then a clean edition), applied to one source's
    answers instead of to the whole map.
    """
    current, answer = _advisory_int(current), _advisory_int(answer)
    if current is None:
        return answer
    if answer is None:
        return current
    for value in (1, 0, 2):
        if value in (current, answer):
            return value
    return current


def _record_advisory(answers, answer):
    """Record one source's ``(value, source)`` answer, keeping its strongest.

    The one place an answer enters the per-source map, so "which of a source's
    several answers counts" is answered once — see `_strongest_advisory`.
    """
    key, value = answer[1], answer[0]
    answers[key] = _strongest_advisory(answers.get(key), value)


def _winning_source(answers, value):
    """The first source (in ask order) whose answer is the merged `value`."""
    for source, answer in (answers or {}).items():
        if _advisory_int(answer) == value:
            return source
    return None


def _itunes_song_advisory(title, artist="", timeout=None):
    """(value, source) from Apple's song search — the LAST resort.

    A search hit is a candidate, not an identity, so a hit is only accepted
    when `title_matches` says it is the file's title AND its own artist credit
    contains the file's artist. `title_matches` is also what refuses an
    instrumental/karaoke/cover hit (and the reverse): search answers those
    generously, and a variant's rating is not this track's. A `cleaned` hit is
    never accepted (see `_apple_value`).
    """
    title = str(title or "").strip()
    if not title:
        return None
    data = _advisory_json(f"{_ITUNES_LOOKUP}/search",
                          {"term": " ".join(t for t in (artist, title) if t),
                           "entity": "song", "limit": 10}, timeout=timeout)
    want_artist = _norm_compare(artist)
    for item in (data or {}).get("results") or []:
        if not title_matches(title, item.get("trackName")):
            continue
        if want_artist and want_artist not in _norm_compare(item.get("artistName")):
            continue
        value = _apple_value(item)
        if value is not None:
            return (value, "itunes-song")
    return None


def _spotify_configured(cfg):
    cfg = cfg or {}
    return bool(str(cfg.get("spotify_client_id") or "").strip()
                and str(cfg.get("spotify_client_secret") or "").strip())


# The tags a track's own video origin is recorded in: the video pipeline
# writes SOURCE, and YOUTUBEID/VIDEOID are the explicit spellings.
_YOUTUBE_TAG_KEYS = ("YOUTUBEID", "YOUTUBE_ID", "VIDEOID", "SOURCE")
_YOUTUBE_ID_RX = re.compile(r"[A-Za-z0-9_-]{11}")


def youtube_video_id(tags):
    """The YouTube id a track's OWN tags record, or "".

    Only a recorded origin counts: an `11`-character id, or a URL carrying
    `v=`/`/shorts/`/`youtu.be/`. Nothing here searches YouTube — a search's
    first hit is not this track, and rating the wrong video is precisely the
    failure this rule exists to prevent.
    """
    if not isinstance(tags, dict):
        return ""
    for key in _YOUTUBE_TAG_KEYS:
        value = str(tags.get(key) or "").strip()
        if not value:
            continue
        if "youtube.com" in value or "youtu.be" in value:
            match = re.search(r"(?:v=|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})",
                              value)
            if match:
                return match.group(1)
            continue
        if _YOUTUBE_ID_RX.fullmatch(value):
            return value
    return ""


def youtube_age_advisory(video_id, cfg=None):
    """(1, "youtube-age") when YouTube itself flags the video 18+, else None.

    Only asked about a video the caller already knows belongs to this track
    (see `youtube_video_id`) or passes itself. `age_limit >= 18` states
    explicit; anything else — a normal video, an unavailable one, yt-dlp not
    installed, YouTube disabled — states NOTHING, because "no age gate" is not
    a statement about the music.
    """
    from server import youtube

    ident = str(video_id or "").strip()
    if not _YOUTUBE_ID_RX.fullmatch(ident):
        return None
    if not youtube.enabled(cfg):
        return None
    module = youtube._load_ytdlp()
    if module is None:
        return None
    try:
        with module.YoutubeDL(youtube._ydl_opts(cfg)) as ydl:
            info = ydl.extract_info(youtube._watch_url(ident), download=False)
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    try:
        limit = int(info.get("age_limit") or 0)
    except (TypeError, ValueError):
        return None
    return (1, "youtube-age") if limit >= 18 else None


def _isrc_codes(isrc):
    """Every ISRC the caller named, in ask order, deduplicated.

    `AudioFile.get_tag` reads repeated fields back "; "-joined (several ISRCs
    on one recording arrive as "A; B"), and a caller may hand over a list of
    its own, so both spellings mean the same thing here: ask EVERY one of
    them. Taking the first alone made the file's own ISRC tag contribute one
    code while this route's contract says "every ISRC the track has".
    """
    raw = list(isrc) if isinstance(isrc, (list, tuple, set)) else [isrc]
    codes = []
    for value in raw:
        for piece in str(value or "").split(";"):
            code = piece.strip()
            if code and code.upper() not in {c.upper() for c in codes}:
                codes.append(code)
    return codes


# How many alias substitutions the advisory route may try once every source
# came up empty under the track's stored names. Each one re-asks the
# name-based routes, so the walk is capped: the whole-name localisation and
# the title alone are what `_alias_queries` puts first.
_MAX_ADVISORY_ALIAS_QUERIES = 2


def _advisory_alias_queries(artist, title, album, recording_mbid, tags, cfg):
    """Ordered ``(artist, title, album)`` substitutions from MB aliases, or [].

    The names MusicBrainz states for the track's entities besides the stored
    ones (`search_aliases`, the reader's locale ladder order), turned into
    substitutions by the SAME capped walk the lyrics chain uses
    (`mlo.lyrics_providers._alias_queries`: the whole name localised first,
    then each entity on its own).

    Only entities the track names by MBID are asked — the recording id (the
    route's `recording_mbid`, or the file's own MUSICBRAINZ_TRACKID tag), the
    artist id and the release-group id out of `tags`. A NAME-only MusicBrainz
    search is deliberately not made: an unnamed track must not cost a search
    per entity, which is also what keeps a route the caller gave no identities
    to completely offline. Never raises; [] when MusicBrainz states nothing.
    """
    try:
        from mlo.lyrics_providers import _alias_queries
    except Exception:
        return []
    tags = tags if isinstance(tags, dict) else {}

    def tag(*names):
        for name in names:
            value = str(tags.get(name) or "").strip()
            if value:
                return value
        return ""

    names = {}
    recording = str(recording_mbid or "").strip() or tag("MUSICBRAINZ_TRACKID")
    if recording and title:
        found = search_aliases("recording", recording, cfg, title)
        if found:
            names["title"] = found
    artist_id = tag("MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_ALBUMARTISTID")
    if artist_id and artist:
        found = search_aliases("artist", artist_id, cfg, artist)
        if found:
            names["artist"] = found
    group_id = tag("MUSICBRAINZ_RELEASEGROUPID")
    if group_id and album:
        found = search_aliases("release-group", group_id, cfg, album)
        if found:
            names["album"] = found
    # The file's OWN alias tags join LAST and need no id at all: a track whose
    # alias was written to it (because its name is published in another
    # language) but which carries no MusicBrainz ids has no other way to be
    # asked about under that name. In the common case the value is already in
    # the MB list above — this is what covers the file that has no ids.
    try:
        from mlo.audio import file_aliases
        for slot, values in file_aliases(tags).items():
            merged = names.setdefault(slot, [])
            for value in values:
                if value not in merged:
                    merged.append(value)
    except Exception:
        pass
    if not names:
        return []
    original = (" ".join(str(artist or "").split()),
                " ".join(str(title or "").split()),
                " ".join(str(album or "").split()))
    out = []
    for a_name, t_name, al_name, _entity, _query in _alias_queries(
            artist, title, album, names):
        key = (" ".join(str(a_name or "").split()),
               " ".join(str(t_name or "").split()),
               " ".join(str(al_name or "").split()))
        if key != original and key not in out:
            out.append(key)
    return out[:_MAX_ADVISORY_ALIAS_QUERIES]


def resolve_advisory_route(isrc="", recording_mbid="", title="", artist="",
                           album="", disc=None, track=None, track_count=None,
                           cfg=None, youtube_id="", tags=None):
    """{"value": 0|1|2|None, "source": key|None, "checked": [key, ...],
        "answers": {key: 0|1}}.

    EVERY applicable source is asked, in one pass, and none of them
    short-circuits:

      1. Deezer by ISRC, then 2. Spotify by ISRC — for EVERY ISRC the track
         has: every code on the file's own ISRC tag (see `_isrc_codes`, which
         is also what a caller's list of codes means) plus every ISRC
         MusicBrainz holds for its recording (Apple serves no ISRC lookup, so
         it is reached by edition instead and is still cross-referenced with
         both);
      3. Apple's artist route — artist → its album list → the explicit
         edition → track (disc/track, then a title match);
      4. Apple's song search — title match only;
      5. YouTube's 18+ gate for a video the track records — LAST,
         explicit-only, and silent when its input is absent.

    Every one of these states something about the TRACK. An
    album/edition-level flag (Discogs' Parental-Advisory sticker, which this
    route used to merge in as an explicit `1`) is NOT a per-track statement
    and is not asked: one flagged edition must never rate every track of the
    release explicit.

    When every one of them states NOTHING under the track's stored names, the
    NAME-based routes (3 and 4) are asked again under MusicBrainz's alias
    substitutions — the reader's locale first, the whole name localised before
    a single entity (`_advisory_alias_queries`), original first and only while
    the merge is still None.

    `answers` is the per-source map in ask order, `source` the first source
    that stated the merged value, `checked` every route that was asked. A
    source asked more than once — one ISRC per pressing is the normal case —
    contributes its STRONGEST answer (`_strongest_advisory`), so the later
    pressing's explicit flag cannot lose to the earlier one's clean answer.
    The merge itself is `merge_advisory` — one rule, one place — so `value` is
    1, 0, 2, or None, and None (an empty `answers`) is the only signal that
    nobody stated anything: the caller decides what to do about that
    (`mlo.advisory.decide_advisory` is that decision, used by the import).
    """
    if cfg is None:
        try:
            from mlo.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    codes = _isrc_codes(isrc)
    if recording_mbid:
        # The file's own ISRC is asked first, then every ISRC MusicBrainz
        # holds for the recording — both ISRC sources see all of them.
        for code in recording_isrcs(recording_mbid):
            if code.upper() not in {c.upper() for c in codes}:
                codes.append(code)
    spotify_on = _spotify_configured(cfg)
    checked = []
    answers = {}
    for code in codes:
        checked.append("deezer-isrc")
        answer = _advisory_cached(("deezer-isrc", code.upper()),
                                  lambda c=code: _deezer_advisory(c))
        if answer is not None:
            _record_advisory(answers, answer)
        if spotify_on:
            checked.append("spotify-isrc")
            answer = _advisory_cached(("spotify-isrc", code.upper()),
                                      lambda c=code: _spotify_advisory(c, cfg))
            if answer is not None:
                _record_advisory(answers, answer)

    def _ask_names(a_name, al_name, t_name):
        """Ask the NAME-based routes about ONE spelling of the track.

        Apple's album route and Apple's song search are the sources whose
        answer depends on what the track is CALLED; the ISRC sources above are
        named already, which is why the alias pass below re-runs only this
        half. `checked` records each route as it is asked, in this order.

        A track's flag is only ever taken from a source that states something
        about the TRACK. The Discogs edition's Parental-Advisory sticker used
        to be asked here and merged in as an explicit `1`; it is an
        ALBUM/EDITION-level flag, so one flagged edition rated every track of
        the release explicit — the owner's "tagging tracks as explicit even
        when they aren't". It is no longer a per-track advisory source.
        """
        if a_name and al_name:
            checked.append("apple-album")
            key = ("apple-album", _norm_compare(a_name), _norm_compare(al_name),
                   _advisory_int(disc), _advisory_int(track),
                   _norm_compare(t_name))
            answer = _advisory_cached(key, lambda: _apple_album_advisory(
                a_name, al_name, t_name, disc, track, track_count, cfg=cfg))
            if answer is not None:
                _record_advisory(answers, answer)
        if t_name:
            checked.append("itunes-song")
            key = ("itunes-song", _norm_compare(a_name), _norm_compare(t_name))
            answer = _advisory_cached(
                key, lambda: _itunes_song_advisory(t_name, a_name))
            if answer is not None:
                _record_advisory(answers, answer)

    _ask_names(artist, album, title)
    # The last one is an extra EXPLICIT-only signal, album/edition level and
    # silent when its input is missing: YouTube's own 18+ gate for a video the
    # track already records. It cannot clear a track, and it is named by the
    # video, not by the track's names — so it is asked once here, outside the
    # name-based half.
    video = str(youtube_id or "").strip() or youtube_video_id(tags)
    if video:
        checked.append("youtube-age")
        answer = _advisory_cached(("youtube-age", video),
                                  lambda: youtube_age_advisory(video, cfg))
        if answer is not None:
            _record_advisory(answers, answer)
    value = merge_advisory(answers)
    if value is None:
        # The ORIGINAL names stated nothing anywhere: consult MusicBrainz's
        # aliases (the reader's own ladder order) and re-ask the name-based
        # routes under each substitution, stopping at the first one that
        # states a value. The alias names are only ever read here — a source
        # that answered for the stored names ends the question.
        for a_name, t_name, al_name in _advisory_alias_queries(
                artist, title, album, recording_mbid, tags, cfg):
            _ask_names(a_name, al_name, t_name)
            value = merge_advisory(answers)
            if value is not None:
                break
    return {"value": value, "source": _winning_source(answers, value),
            "checked": checked, "answers": answers}


def resolve_advisory(isrc="", recording_mbid="", **context):
    """ITUNESADVISORY for one track — the merged answer of every source.

    Deezer by ISRC, Spotify by ISRC when configured, Apple's artist→album
    route and Apple's song search are all asked (see
    `resolve_advisory_route`), and the merged value follows `merge_advisory`:
    1 when ANY of them says explicit, else 0 when any says clean, else 2 when
    any says clean edition, else None — nobody stated anything, which is a
    caller's decision, not a value to write.

    `context` may carry `title`, `artist`, `album`, `disc`, `track`,
    `track_count` and `cfg` (the Apple routes need them; the ISRC sources do
    not). `isrc` may be the file's own ISRC tag exactly as `AudioFile.get_tag`
    reads it — several codes "; "-joined — or a list of codes; every one of
    them is asked. MusicBrainz supplies the ISRCs when the caller has only a
    recording ID. Use `resolve_advisory_route` when the per-source answers
    matter too.
    """
    return resolve_advisory_route(isrc=isrc, recording_mbid=recording_mbid,
                                  **context)["value"]


def release_advisories(mbid, sources=None, answers=None):
    """{f'{disc}:{position}': 0|1} for every track of a release.

    Every track gets the merged value of the sources that were asked (0 when
    none of them stated anything — the merge rule, not a gap to fill in).
    `sources` (optional) is filled with the source that stated each value and
    `answers` (optional) with the whole per-track `{source: 0|1}` map, so a
    caller can report provenance alongside the value.

    Every ISRC the release states for a track is asked, not just the first:
    MusicBrainz lists one per pressing, and the route's own contract (and
    `_isrc_codes`) is that all of them are put to the ISRC sources. Taking
    `isrcs[0]` alone let a clean first pressing hide the explicit second one —
    the same miss the file's own ISRC tag had.
    """
    release = release_lookup(mbid)
    fallback_artist = next((a.get("name") for a in release.get("artists") or []
                            if a.get("name")), "")
    track_count = len(release.get("media") or [])
    out = {}
    for t in release.get("media") or []:
        key = f"{int(t.get('disc') or 1)}:{int(t.get('position') or 0)}"
        route = resolve_advisory_route(
            isrc=t.get("isrcs") or "",
            recording_mbid=t.get("recording_mbid") or "",
            title=t.get("title") or "",
            artist=t.get("artist_credit") or fallback_artist,
            album=release.get("title") or "",
            disc=t.get("disc"),
            track=t.get("position"),
            track_count=track_count,
        )
        if route["value"] is not None:
            out[key] = route["value"]
            if sources is not None and route["source"]:
                sources[key] = route["source"]
            if answers is not None and route.get("answers"):
                answers[key] = route["answers"]
    return out


def release_group_genres(rg_mbid):
    try:
        data = mb_get_cached(f"release-group/{rg_mbid}", {"inc": "genres", "fmt": "json"})
        return _genres(data)
    except Exception:
        return []


def artist_genres(artist_mbid):
    try:
        data = mb_get_cached(f"artist/{artist_mbid}", {"inc": "genres", "fmt": "json"})
        return _genres(data)
    except Exception:
        return []


def recording_genres(recording_mbid):
    """A recording's OWN genres — the track-level sibling of `artist_genres`.

    MusicBrainz states genres on the recording entity too, and a track page
    has a recording id in hand, so this asks about the track itself rather than
    its artist. Most recordings state none (genres are voted at artist and
    release-group level far more often), so a caller reads an empty list as
    "nothing stated HERE" and falls back to the artist.
    """
    try:
        data = mb_get_cached(f"recording/{recording_mbid}", {"inc": "genres", "fmt": "json"})
        return _genres(data)
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# The ONE album-level read: MusicBrainz's RECORDING BROWSE
# --------------------------------------------------------------------------- #
# A browse answers a whole RELEASE at once — every recording it holds, each with
# its own `genres` and `rating` — where a lookup answers one recording. The
# album's track count is what used to decide the price of a track's rating or
# its genres' WORK fallback, because both were read per recording; they read
# THIS instead, so an album costs one request (one page) however many tracks it
# has.
#
# VERIFIED live (2026-10-12, The Dark Side of the Moon, release
# 8d17f5db-b9d0-4e35-9a06-9e1bffebc5be), asked with the app's own User-Agent:
# `GET recording?release=<id>&inc=genres+ratings+work-rels&limit=100&fmt=json`
# answers `recording-count: 9` and 9 rows, each carrying
# `genres[{name,id,count}]`, `rating{value,votes-count}` AND its
# `relations[].work.id` — `inc=work-rels` IS accepted on the browse, so the work
# ids ride along and no per-track identity request is needed to find them.
# (Measured: 0.80 s for the 9 tracks.)
_BROWSE_INC = "genres+ratings+work-rels"
_BROWSE_PAGE = 100
# honey: the browse walk stops after this many recordings of one release — a
# bound on a broken `recording-count` (the loop is otherwise server-driven),
# not a policy. Every real album is orders of magnitude below it (a 100-disc box
# set is ~1000 rows); revisit if a release MusicBrainz reports beyond this ever
# needs every row (a truncated index only costs the per-track fallback).
_BROWSE_MAX_ROWS = 10000


def release_recording_index(release_mbid):
    """``{recording_mbid: recording node}`` for one RELEASE — ONE browse, paged.

    The album-level reader both callers share: `web_rating_fetchers` (mlo
    script 24's rating ladder) and `_genre_source_answers` (the genre chain's
    track tier). Keyed by the RELEASE's own mbid — a release group is not
    browsable this way, and one pressing's recording list is what the tags
    name.

    Paging follows MusicBrainz's own `recording-count`: the first page asks
    `limit=100`, and further pages are read (same 1 req/s etiquette) only while
    the rows served fall short of the count. Every page goes through
    `mb_get_cached`, so the TTL, the single-flight join and the throttle are the
    ones every other MusicBrainz read already obeys — a second album pass costs
    no request, and two threads asking about one release share one flight.

    A failure (transport, refusal, a page that will not parse) ends the walk and
    returns what was read — never raises: a caller treats a missing recording as
    "the browse did not answer for it" and falls back to its per-track read.
    """
    rid = str(release_mbid or "").strip()
    if not rid:
        return {}
    out, offset = {}, 0
    while True:
        try:
            data = mb_get_cached("recording", {"release": rid, "inc": _BROWSE_INC,
                                               "limit": _BROWSE_PAGE,
                                               "offset": offset, "fmt": "json"})
        except Exception:
            break
        if not isinstance(data, dict):
            break
        rows = [r for r in (data.get("recordings") or []) if isinstance(r, dict)]
        for row in rows:
            rec_id = str(row.get("id") or "")
            if rec_id:
                out[rec_id] = row
        offset += len(rows)
        try:
            count = int(data.get("recording-count") or 0)
        except (TypeError, ValueError):
            count = 0
        if not rows or offset >= count or offset >= _BROWSE_MAX_ROWS:
            break
    return out


def release_group_rating(rg_mbid):
    """The release GROUP's own rating node, or None.

    The ALBUM-level MusicBrainz source the owner asked for: a release group is
    what "the album" means to MusicBrainz, and its rating is the community's
    score for the record rather than for one pressing (a release is an
    edition; `inc=ratings` on the release itself states nothing). VERIFIED
    live: `release-group/f5093c06…` answers
    ``{"rating": {"value": 4.75, "votes-count": 105}, "genres": [...]}``.

    The whole NODE is returned, not a number: ``mlo.web_ratings`` owns the
    interpretation (the 0-5 to 0-100 conversion and the vote-count weight), and
    one payload shape — MusicBrainz's own — keeps that parser identical for the
    album and the track. Through `mb_get_cached`, so the rating, the genres and
    any other reader of the same group share the one request.
    """
    if not rg_mbid:
        return None
    return mb_get_cached(f"release-group/{rg_mbid}", {"inc": "ratings", "fmt": "json"})


def artist_rating(artist_mbid):
    """The ARTIST's own rating node, or None — the rating ladder's last rung.

    The owner's rule: a track reads the RECORDING, then the ALBUM (the release
    group, this time when the recording states none), and the ARTIST only when
    neither is rated (`mlo.web_ratings._rate`). The artist entity is the one
    MusicBrainz puts the artist's own votes on (`inc=ratings`; the node shape
    is the same `{"rating": {...}}` the other two answer with, so the parser is
    the one `parse_musicbrainz`). Through `mb_get_cached`, like every other
    read: one request per artist, ever, and none at all when its album or
    recording already answered.
    """
    if not artist_mbid:
        return None
    return mb_get_cached(f"artist/{artist_mbid}", {"inc": "ratings", "fmt": "json"})


def _recording_work_id(node):
    """The WORK a recording NODE performs, or "" — from its own relations.

    MusicBrainz states the performance relation on the recording; it rides
    along both in the per-recording lookup (`inc=work-rels`) and in the album
    BROWSE (`release_recording_index`), so the id is read here for either
    shape.
    """
    for rel in (node or {}).get("relations") or []:
        work_id = str((rel.get("work") or {}).get("id") or "")
        if work_id:
            return work_id
    return ""


def _recording_payload(node):
    """``{"recording": node, "work": node|None}`` — the work only when needed.

    The ONE rule both readers of a recording share: the work is fetched only
    when the recording states NO rating of its own, because that is the only
    case its answer matters in — measured on The Dark Side of the Moon, the
    recording is rated 4/4 times and the work 1/4, so asking every work up
    front would pay a rate-limited request for a fallback that almost never
    answers. `node` comes from the per-recording lookup OR from the album
    browse (`release_recording_index`), and the work id it carries is what
    saves that lookup's own search for it.

    A recording that states no rating AND performs no work is a plain miss —
    both halves stay None and the source contributes nothing.
    """
    payload = {"recording": node, "work": None}
    if not isinstance(node, dict):
        return payload
    if (node.get("rating") or {}).get("value") is not None:
        return payload
    work_id = _recording_work_id(node)
    if work_id:
        try:
            payload["work"] = mb_get_cached(f"work/{work_id}",
                                            {"inc": "ratings", "fmt": "json"})
        except Exception:
            payload["work"] = None
    return payload


def _recording_with_work(recording_mbid):
    """``{"recording": node, "work": node|None}`` for one recording.

    ONE request answers the recording's own rating, its genres and the WORK it
    performs (``inc=ratings+genres+work-rels``, VERIFIED live: 4 of 4 Dark
    Side of the Moon recordings came back rated AND with a work id). This is the
    PER-TRACK read — `web_rating_fetchers` reaches it when the album browse
    (`release_recording_index`) did not answer for this recording, or when the
    caller knows no release id at all — and the work handling is the shared one
    (`_recording_payload`).
    """
    if not recording_mbid:
        return None
    node = mb_get_cached(f"recording/{recording_mbid}",
                         {"inc": "ratings+genres+work-rels", "fmt": "json"})
    return _recording_payload(node)


def recording_work_genres(recording_mbid):
    """The genres MusicBrainz states on the WORK a recording performs.

    The WORK is the TRACK tier's fallback behind the recording — asked only
    when the recording itself states no genre: the composition is classified
    once for every recording of it, but the recording is the entity the track
    IS (R360). The work is reached in the one request shape this module
    already uses for a recording's identity (`_mb_recording_ids`,
    `inc=url-rels+work-rels`), and its genres are a second cached request —
    ``inc=genres`` — because MusicBrainz has no inline "work genres" include
    (`work-level-rels` inlines a work's RELATIONS, not its genre votes).

    MEASURED, live, on The Dark Side of the Moon (2026-10-01): 0 of 4 works
    stated any genre, while all 4 recordings stated 5-9 of their own. The work
    tier is therefore usually empty, and leading with it (as the chain used to)
    both lost the recording's own answer's precedence and paid two
    rate-limited MusicBrainz requests per track for a tier that never spoke —
    which is why the caller asks for it only when the recording is silent.
    Returns [] for a recording with no work, no genres, or no answer at all.

    The caller caches the RESULT (`_genre_cached`, keyed by recording), so the
    two lookups below run once per recording per 30 days; this helper does not
    add a second cache layer of its own.
    """
    if not recording_mbid:
        return []
    ids = _mb_recording_ids(recording_mbid) or {}
    work = str(ids.get("work") or "")
    if not work:
        return []
    return work_genres(work)


def work_genres(work_mbid):
    """The genres MusicBrainz states on a WORK, by its own id.

    The second half of `recording_work_genres`, split out because the album
    browse (`release_recording_index`) already carries each recording's work id
    (`inc=work-rels` is accepted on the browse): a caller holding that id reaches
    the work's genres in ONE cached request instead of the two the per-recording
    route costs (an identity lookup for the id, then this one). Returns [] for a
    work nobody classified, or one MusicBrainz will not answer about.
    """
    if not work_mbid:
        return []
    try:
        node = mb_get_cached(f"work/{work_mbid}", {"inc": "genres", "fmt": "json"})
    except Exception:
        return []
    return _genres(node or {})


def genre_cascade(release, limit=None):
    """Cascading genre import: track -> release-group -> artist, ONE tier each.

    A track's own RECORDING answers first (`trk["genres"]`, the recording's
    entity genres from the release payload), then the release GROUP's, then the
    ARTIST's — and the first tier that states anything supplies the whole list,
    deduped, in MusicBrainz's own (vote-count) order and capped at `limit`
    (limit=None imports everything). The release's own (edition) genres are
    never read: the album IS its group, not one pressing (the owner's rule,
    2026-10-12). Returns per-track genres plus the tier that answered each
    track (`source`) and the tiers that had anything at all (`levels`). The
    caller that writes these to a file canonicalizes them
    (`_write_album_genres` in server.main), so the cap and the hierarchy are
    applied once: the tag keeps the names, the levels are derived.

    This is the PREVIEW half of the policy — it reads only the payload in hand.
    The import chain (`_genre_source_answers`) applies the same tiers and
    additionally reads the WORK when the recording states no genres (which
    costs a request per silent recording); a cascade answer can therefore be
    one tier narrower than an import's on such a track.
    """
    rg = release.get("release_group_id")
    rg_genres = release_group_genres(rg) if rg else []
    artist_genres_all = []
    for a in release.get("artists", []):
        if a.get("mbid"):
            artist_genres_all.extend(artist_genres(a["mbid"]))
    artist_genres_all = list(dict.fromkeys(artist_genres_all))

    per_track = []
    for trk in release.get("media", []):
        used = None
        names = []
        for level, lst in (
            ("track", trk.get("genres") or []),
            ("release-group", rg_genres),
            ("artist", artist_genres_all),
        ):
            if lst:
                used, names = level, list(lst)
                break
        merged = names[:limit] if limit else names
        per_track.append({
            "position": trk["position"],
            "disc": trk["disc"],
            "title": trk["title"],
            "genres": merged,
            "source": used if merged else None,
            "levels_used": [used] if merged else [],
        })
    return {
        "per_track": per_track,
        "levels": {
            "track": any(t.get("genres") for t in release.get("media", [])),
            "release_group": bool(rg_genres),
            "artist": bool(artist_genres_all),
        },
    }


# --------------------------------------------------------------------------- #
# Web ratings — the payloads `mlo.web_ratings` asks for
# --------------------------------------------------------------------------- #
# The rating layer lives in `mlo.web_ratings` (parsers, aggregation, writer,
# script 24) and never imports this module: it is handed ONE callable —
# `web_rating_fetchers()` — and calls it per source. Everything below is the
# network half of that contract, and every function returns the source's RAW
# payload (or None) so the interpretation stays in the one place that owns the
# 0-100 scale.
def web_rating_fetchers():
    """The ONE callable `mlo.web_ratings` drives: ``fetch(source, kind,
    ident, cfg)``.

    `ident` is the entity the source is asked about, and `mbid` inside it is
    THAT level's own MusicBrainz id: ``{"artist", "title", "mbid"}`` for a
    track (the recording), ``{"artist", "album", "mbid"}`` for an album (the
    release GROUP), ``{"artist", "mbid"}`` for the artist. The answer is that
    source's RAW payload or None. MusicBrainz is the only source: it is a
    documented API that needs no credential, so an identity with no MBID (the
    artist rung of a folder whose files name no artist id, say) answers None
    WITHOUT a request.

    A TRACK ask also carries the RELEASE's mbid (``ident["release_mbid"]``,
    from the file's own MUSICBRAINZ_ALBUMID) whenever the caller knows it: the
    recording's rating then comes from the album's ONE browse
    (`release_recording_index`) instead of a lookup per track, and only a
    recording that browse did not answer for falls back to its own lookup. No
    release id, no browse row, or a browse that failed = the per-track path,
    exactly as it was.
    """
    def fetch(source, kind=None, ident=None, cfg=None):
        source = str(source or "").strip().lower()
        ident = ident or {}
        mbid = str(ident.get("mbid") or "").strip()
        level = str(kind or "album")
        if source == "musicbrainz":
            if not mbid:
                return None
            if level == "track":
                release_mbid = str(ident.get("release_mbid") or "").strip()
                if release_mbid:
                    node = release_recording_index(release_mbid).get(mbid)
                    if node is not None:
                        return _recording_payload(node)
                return _recording_with_work(mbid)
            if level == "artist":
                return artist_rating(mbid)
            return release_group_rating(mbid)
        return None

    return fetch


# --------------------------------------------------------------------------- #
# MusicBrainz query terms and recording aliases
# --------------------------------------------------------------------------- #


def _mb_query(value):
    """A name as a MusicBrainz Lucene term: a quote or backslash inside the
    quoted phrase would otherwise break the whole query."""
    return re.sub(r'["\\]', " ", str(value or "")).strip()


def attach_recording_aliases(release, mbid=""):
    """Fill `tracks[].alias` on a release payload from ONE browse call.

    MusicBrainz has no nested `inc` for aliases: a release lookup's
    `inc=recordings` carries the recordings but never their aliases, and a
    lookup per track would spend a request per row on a 1 req/s budget. The
    BROWSE endpoint answers the whole release at once (`recording?release=<id>
    &inc=aliases`, 100 rows a page) and is cached like every other MusicBrainz
    read, so a release page costs one request the first time and none after.

    Returns how many rows got an alias; a payload that fails to arrive leaves
    every track as it was — no alias is better than a broken page.
    """
    # The payload carries the tracklist TWICE: `tracks` (the flat list the
    # wizard and the importers read) and `media[].tracks` (the per-disc shape
    # the release page renders), so both get the alias.
    rows = [t for t in (release.get("tracks") or []) if isinstance(t, dict)]
    for medium in (release.get("media") or []):
        if isinstance(medium, dict):
            rows.extend(t for t in (medium.get("tracks") or []) if isinstance(t, dict))
    rid = str(mbid or release.get("id") or "").strip()
    if not rows or not rid:
        return 0
    try:
        data = mb_get_cached("recording", {"release": rid, "inc": "aliases",
                                           "limit": 100, "fmt": "json"})
    except Exception:
        return 0
    by_id = {r.get("id"): r for r in (data.get("recordings") or [])}
    filled = 0
    for track in rows:
        row = by_id.get(track.get("recording_mbid"))
        if not row:
            continue
        alias = alias_for(row.get("aliases"), None, track.get("title"))
        if alias:
            track["alias"] = alias
            filled += 1
    return filled


# --------------------------------------------------------------------------- #
# Bandcamp (scraped — no public API)
# --------------------------------------------------------------------------- #
# An album page carries the release's crowd-sourced tags as `.tralbum-tags`
# anchors, and its own JSON blob (`data-tralbum`) holds the album title plus
# one `trackinfo` entry per track. Bandcamp states NO per-track genre at all
# — every tag it has is the album's — so this source is an ALBUM-level
# crowdsourced tier and the chain labels its row `level: album`.
#
# VERIFIED here: bandcamp.com's own search/tag pages answer this client with
# Cloudflare's interstitial, but the per-artist album pages do NOT — and the
# album URL is derivable from the names
# (`https://<artist-slug>.bandcamp.com/album/<album-slug>`), so the source
# costs one request per album. A page is accepted only when it states THIS
# album: the tralbum's own title matches, and (when the caller knows its own
# track titles) the page's track titles line up with them — a wrong album's
# tags are NO answer. Politeness is a 1 req/s pace, browser-like headers, one
# line per process on a hard failure, and the caller's 30-day cache wraps the
# whole source so a repeat import re-fetches nothing.
BANDCAMP_BASE = "https://bandcamp.com"
BANDCAMP_MIN_INTERVAL = 1.0
_bandcamp_lock = threading.Lock()
_bandcamp_last = 0.0
_bandcamp_warned = False
_BANDCAMP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BANDCAMP_BASE + "/",
}
# The attribute value is HTML-escaped (`&quot;`), so no raw `"` can appear
# inside it and a plain non-greedy match is safe.
_BANDCAMP_TRALBUM_RE = re.compile(r'data-tralbum="([^"]*)"', re.S)
# Tags are `<a class="tag" href=".../discover/<slug>">Label</a>` inside the
# tag block. Read as labels; the block is found by its own class so no anchor
# elsewhere on the page can be mistaken for a tag.
_BANDCAMP_TAG_RE = re.compile(r'<a class="tag"[^>]*>([^<]+)</a>', re.I)


def _bandcamp_unreachable(reason):
    """One concise line per process when Bandcamp cannot answer at all."""
    global _bandcamp_warned
    if _bandcamp_warned:
        return
    _bandcamp_warned = True
    print(f"[mlo] bandcamp.com: {reason} — that source is skipped this run")


def _bandcamp_get(url, timeout=20.0):
    """One polite GET of a Bandcamp page (browser UA, 1 req/s), or "".

    "" covers everything that is not that page: a 404 slug, an empty body, a
    connection failure. A 404 is a miss the caller moves on from, so only a
    connection failure is logged.
    """
    global _bandcamp_last
    with _bandcamp_lock:
        wait = BANDCAMP_MIN_INTERVAL - (time.time() - _bandcamp_last)
        if wait > 0:
            time.sleep(wait)
        _bandcamp_last = time.time()
        try:
            r = httpx.get(url, headers=_BANDCAMP_HEADERS, timeout=timeout,
                          follow_redirects=True)
        except httpx.HTTPError as e:
            _bandcamp_unreachable(f"no connection ({type(e).__name__})")
            return ""
    if r.status_code != 200 or not r.text:
        return ""
    return r.text


def _bandcamp_slug(value, sep):
    """`value` as a Bandcamp slug: folded, `&` → `and`, other junk → *sep*."""
    text = str(value or "").strip().lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", sep, text).strip(sep)


def _bandcamp_subdomain_candidates(artist):
    """The Bandcamp subdomains *artist* may use, best first.

    Bandcamp subdomains are written without separators ("King Buffalo" →
    `kingbuffalo`); the hyphenated form is kept as a second candidate because
    some artists do register that way.
    """
    out = [_bandcamp_slug(artist, ""), _bandcamp_slug(artist, "-")]
    return [s for i, s in enumerate(out) if s and s not in out[:i]]


def _bandcamp_tralbum(page):
    """The page's `data-tralbum` JSON, or None when the page carries none."""
    match = _BANDCAMP_TRALBUM_RE.search(page or "")
    if not match:
        return None
    try:
        data = json.loads(_html.unescape(match.group(1)))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _bandcamp_tracks(tralbum):
    """[{position, title}] from a tralbum's `trackinfo` (its own track list)."""
    out = []
    for row in (tralbum or {}).get("trackinfo") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if title:
            out.append({"position": row.get("track_num"), "title": title})
    return out


def _bandcamp_tags(page):
    """The album's own tags, junk filtered like ListenBrainz free tags.

    A tag is kept when it is not a mood word (`LB_MOOD_WORDS`) and is not the
    same word as one already kept once punctuation and spacing are ignored —
    Bandcamp lists literal duplicates of its own slugs side by side ("doom
    metal" and "doommetal"), and those are one tag, not two.
    """
    from server import discovery

    start = page.find("tralbum-tags tralbum-tags-nu")
    if start < 0:
        start = page.find("tralbumData tralbum-tags")
    if start < 0:
        return []
    end = page.find("</div>", start)
    block = page[start:end if end > start else len(page)]
    out, seen = [], set()
    for match in _BANDCAMP_TAG_RE.finditer(block):
        name = _html.unescape(match.group(1)).strip()
        key = re.sub(r"[^a-z0-9]+", "", name.lower())
        if not name or not key or key in seen:
            continue
        if name.lower() in discovery.LB_MOOD_WORDS:
            continue
        seen.add(key)
        out.append(name)
    return out


def _bandcamp_is_album(tralbum, album, titles=None):
    """Whether a tralbum is the album we asked about (never a guess)."""
    from server import discovery

    if album:
        stated = discovery.norm((tralbum.get("current") or {}).get("title"))
        if stated != discovery.norm(album):
            return False
    page_titles = {discovery.norm(t["title"]) for t in _bandcamp_tracks(tralbum)}
    ours = [discovery.norm(t) for t in titles or [] if str(t or "").strip()]
    if ours and page_titles and not (set(ours) & page_titles):
        return False
    return True


def bandcamp_album(artist="", album="", titles=None):
    """Bandcamp's tags for one album: {"genres", "tracks", "url"} or None.

    `titles` (optional) are the caller's own track titles for that album: they
    are what confirms the page that came back is this release, so a
    same-named record by another band cannot donate its tags. None means "no
    such album for this client" (a wrong slug, no Bandcamp presence, the
    interstitial) — never an empty answer dressed up as one.
    """
    name = str(album or "").strip()
    if not name:
        return None
    for sub in _bandcamp_subdomain_candidates(artist):
        url = f"https://{sub}.bandcamp.com/album/{_bandcamp_slug(name, '-')}"
        page = _bandcamp_get(url)
        if not page:
            continue
        tralbum = _bandcamp_tralbum(page)
        if not tralbum or not _bandcamp_is_album(tralbum, name, titles):
            continue
        return {"genres": _bandcamp_tags(page),
                "tracks": _bandcamp_tracks(tralbum), "url": url}
    return None


# --------------------------------------------------------------------------- #
# Genre chain — one merge point for every genre source
# --------------------------------------------------------------------------- #
# Order used when mlo.config `genre_sources` is empty. It is a PRIORITY LIST:
# the sources are asked in this order until every track is full — the merge
# takes each answer in this order and STOPS once the writer's own policy can
# write a complete list from what it has (see `_genre_complete`), so the
# position of a source is what a track's genre picks look like and the ones
# below it are the fallbacks, not a second opinion.
# `mlo.config.DEFAULT_CONFIG["genre_sources"]` is this list and
# `normalize_config` migrates the old shipped defaults into it, dropping every
# id this build no longer knows (`tools/test_genres.py` asserts the two lists
# are equal).
#
# These are the ONLY sources the app asks for genres — the owner's rule, and
# MusicBrainz is the only one left (2026-10-12): the app no longer scrapes
# other sites for genres — their terms forbid it and the cookie/challenge
# machinery that served them was the least maintainable part of the app.
# MusicBrainz is a documented API that needs no credential and is never
# challenged, and it answers at the level the entity is entitled to, falling
# back recording (its work only when the recording states none) → release
# GROUP → artist — one tier supplies a track's list, never a blend. The list
# stays a list
# because `genre_sources` (the user's own list) is read against it, and
# `mlo.config._GENRE_SOURCE_IDS` is exactly this set (the migration drops any
# other id a saved config carries).
GENRE_SOURCES = ["musicbrainz"]


# The key an ALBUM- or ARTIST-wide answer is filed under: it applies to every
# track of the release, and the row's `level` is what says so.
_ALL_TRACKS = ""
_LEVEL_RANK = {"track": 0, "album": 1, "artist": 2}


def _genre_track_key(disc, position):
    """`"disc:position"` — the key one track's answer is filed under."""
    return f"{int(disc or 1)}:{int(position or 0)}"


def _genre_names(names):
    """Trimmed, case-insensitively deduped names, original spelling kept."""
    out = []
    for name in names or []:
        text = str(name or "").strip()
        if text and text.lower() not in {g.lower() for g in out}:
            out.append(text)
    return out


def _display_genres(names):
    """A genre list as a PAGE draws it: the app's own capitalization.

    MusicBrainz publishes its genre names lowercase ("shoegaze", "art pop") and
    that is a database convention, not a caption — `_genre_names` keeps what it
    publishes, which is what the genre CASCADE must hand the writers
    (`mlo.genres.normalize_genres` capitalizes once, at the one place that
    writes a tag). An entity page's chip row is a caption instead, so its
    payload goes through `mlo.genres.display_name` — the same capitalization
    the tag writers, the grader and server.discover already share — and the
    name a reader copies off a page is the name a tag holds.

    Capitalization is display, never identity: every comparison downstream
    folds case (`mlo.genres`).
    """
    return [_genre_display(n) or n for n in names]


def _genre_row(level, names, title=""):
    """{"level", "genres"[, "title"]} for one answer, or None when empty."""
    clean = _genre_names(names)
    if not clean:
        return None
    row = {"level": level, "genres": clean}
    if title:
        row["title"] = str(title)
    return row


def _genre_source_skip(source, cfg):
    """WHY a source is not asked at all, or None when it is asked.

    MusicBrainz is the chain's only source and needs no credential — it is a
    documented API that is never challenged — so no source is ever skipped
    before a request. The hook stays because the chain's report reads it: a
    source that could not answer is named in `skipped`, never reported as
    "no data".
    """
    return None


def _genre_complete(names, limit):
    """Whether *names* fill every slot the writer would write.

    This is the chain's one notion of "the answer is already there", and it is
    judged by the policy the WRITERS apply (`mlo.genres.normalize_genres`):
    `limit` stored names. No derived value sits in the list any more, so none
    can stand in for a name a source never stated — at
    `mb_genre_count = 2` a track needs two genres (or one, from a source that
    answers at TRACK level — see `tracks_full`), where the old rule accepted
    one genre plus its own derivable head.
    """
    if not names:
        return False
    try:
        from mlo.genres import normalize_genres
        got = normalize_genres(names, limit)
    except Exception:
        return False
    return len(got) >= max(1, int(limit))


def _genre_needs_ai(genres, limit, level=None):
    """Whether the model is the LAST RESORT for a track — the owner's rule.

    "AI should only be used as fallback when no provider responded with info."
    The model is therefore asked exactly when the sources did not SETTLE the
    track — the same predicate the chain's early stop applies (`settled`): the
    list fills the writer's cap, OR the tier that answered is the track's own
    (a track-level answer is complete by itself, R37). A track the recording
    itself classified is therefore never handed to the model; one the sources
    left at ALBUM/ARTIST level with fewer names than the cap is, because that
    answer cannot fill the slots the writer would write. For the release-wide
    summary (`level=None`) the cap alone is the test.
    """
    if level == "track":
        return False
    return not _genre_complete(genres, limit)


# --------------------------------------------------------------------------- #
# 30-day value cache — genre answers (per album / recording) and, through the
# same helper, the cover-search image dimension probes (per image URL)
# --------------------------------------------------------------------------- #
# Genre data moves slowly, one album pass asks the same album (and the same
# recording) over and over, and every crowdsourced source here is throttled to
# ~1 req/s; an image's size likewise never changes for a URL. Answers —
# including "nothing", which must not be re-asked either — are kept in memory
# and on disk under <music>/.mlo/data/genre_cache, keyed by kind so the two
# users never collide.
_GENRE_CACHE_TTL = 30 * 86400.0
_GENRE_CACHE: dict = {}
_GENRE_CACHE_LOCK = threading.Lock()
_GENRE_CACHE_MAX = 5000
_GENRE_MISS = object()
# A producer's "this source refused TRANSIENTLY right now" (a Cloudflare
# challenge, a dead connection) — the opposite of `_GENRE_MISS` above. A MISS
# is "asked, and it has nothing", which is worth the full TTL; a refusal is a
# state the user can change (paste the cookie), and remembering it as a MISS is
# what kept a fresh credential from fixing a source for a month.
_GENRE_TRANSIENT = object()


def _genre_cache_file(ident):
    import hashlib

    folder = _data_cache_dir("genre_cache")
    if not folder:
        return None
    return os.path.join(folder, hashlib.sha1(ident.encode("utf-8")).hexdigest()
                        + ".json")


def _genre_cached(kind, key, producer):
    """`producer()` memoized 30 days by (kind, key) — memory, then disk.

    The app's one 30-day disk cache. `kind` is "album" (identity: artist +
    album, or a release-group/artist MBID), "recording" (identity: a recording
    MBID or artist + title) or "cover_dim" (identity: an image URL).
    `producer` must return JSON-serializable data or None and must never
    raise: a failing source is not allowed to take the chain down, and "no
    answer" is cached like any other answer so a rerun costs no request.

    The ONE exception is `_GENRE_TRANSIENT`: a producer that returns it says
    the source REFUSED this request (a challenge, a dead connection) rather
    than having nothing — so it is passed through as None and NOT remembered,
    because a refusal is the user's credential to change and a cached one would
    outlive the fix.
    """
    ident = f"{kind}|{key}"
    with _GENRE_CACHE_LOCK:
        hit = _GENRE_CACHE.get(ident)
    if hit is not None:
        return None if hit is _GENRE_MISS else hit
    path = _genre_cache_file(ident)
    if path:
        try:
            if (os.path.isfile(path)
                    and time.time() - os.path.getmtime(path) < _GENRE_CACHE_TTL):
                with open(path, encoding="utf-8") as fh:
                    value = json.load(fh)
                with _GENRE_CACHE_LOCK:
                    _GENRE_CACHE[ident] = _GENRE_MISS if value is None else value
                return value
        except (OSError, ValueError):
            pass
    value = producer()
    if value is _GENRE_TRANSIENT:
        return None
    with _GENRE_CACHE_LOCK:
        if len(_GENRE_CACHE) >= _GENRE_CACHE_MAX:
            _GENRE_CACHE.clear()
        _GENRE_CACHE[ident] = _GENRE_MISS if value is None else value
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(value, fh)
            os.replace(tmp, path)
        except OSError:
            pass
    return value


# --------------------------------------------------------------------------- #
# Per-source answers
# --------------------------------------------------------------------------- #
def _mb_wikidata_qid(mbid, entity="release-group"):
    """The Wikidata entity a MusicBrainz entity points at, or None.

    MusicBrainz holds the relation itself (`inc=url-rels`), which is what
    makes the Wikidata tier an identity lookup instead of a title guess.
    `entity` is a MusicBrainz endpoint name ("release-group", "recording",
    "work"): the genre chain asks a RECORDING first, then the WORK it performs
    (see `_mb_recording_ids`), because a track's own item carries P136 where
    the release group carries none.
    """
    if not mbid:
        return None
    try:
        data = mb_get_cached(f"{entity}/{mbid}",
                             {"inc": "url-rels", "fmt": "json"})
    except Exception:
        return None
    return _mb_wikidata_qid_in((data or {}).get("relations"))


def _mb_wikidata_qid_in(relations):
    """The Wikidata QID of a MusicBrainz relation list, or ""."""
    for rel in relations or []:
        if str(rel.get("type") or "").strip().lower() != "wikidata":
            continue
        match = re.search(r"Q\d+",
                          str((rel.get("url") or {}).get("resource") or ""))
        if match:
            return match.group(0)
    return ""


def _mb_recording_ids(recording_mbid):
    """{"qid", "work"} for one MusicBrainz recording, both "" when none.

    ONE request (`inc=url-rels+work-rels`) answers both questions: whether the
    recording itself links a Wikidata item (rare — every popular recording
    probed on this machine had no such link), and which WORK it performs. The
    work is the practical route and it is still an identity, never a title
    guess: VERIFIED here, Queen's "Bohemian Rhapsody" recording → work
    `41c94a08…` → Wikidata Q187745, whose P136 is progressive rock / hard rock
    / progressive pop, and Pink Floyd's "Time" → work → Q641913 ("popular
    music"). A multi-word ENTITY SEARCH is not usable for this at all —
    `wbsearchentities` matches labels by prefix, so "artist title" answers
    nothing (VERIFIED: no hits for four-word research queries).
    """
    if not recording_mbid:
        return {"qid": "", "work": ""}
    try:
        data = mb_get_cached(f"recording/{recording_mbid}",
                             {"inc": "url-rels+work-rels", "fmt": "json"})
    except Exception:
        return {"qid": "", "work": ""}
    relations = (data or {}).get("relations") or []
    work = ""
    for rel in relations:
        if work:
            break
        work = str((rel.get("work") or {}).get("id") or "")
    return {"qid": _mb_wikidata_qid_in(relations), "work": work}


# --------------------------------------------------------------------------- #
# Credits / performers
# --------------------------------------------------------------------------- #
# MusicBrainz keeps the players on the RECORDING: `artist-rels` returns one
# relation per person — the relation TYPE is the role (performer, instrument,
# vocal, producer, engineer, mix, mastering, arranger, composer, lyricist,
# conductor, remixer, …), the instrument or vocal part rides in `attributes`
# ("double bass", "lead vocals"), and the person's name + MBID come along in
# the same relation, so no second request is needed to name them. `work-rels`
# names the WORK the recording performs, and `work-level-rels` inlines that
# work's OWN relations — where MusicBrainz keeps a work's composer, lyricist
# and writer — into the same response, so the songwriting half of a track's
# credits (and of EVERY recording's work of an album) costs no extra request.
# ONE request per recording, ONE per release (`recording-level-rels` +
# `work-level-rels` bring each track's own relations with the release, VERIFIED
# against MB while writing this), both through mb_get_cached so the cache and
# the 1 req/s etiquette apply like any other MB call.
CREDITS_MAX_RECORDINGS = 200   # a box set must not become 200 requests' worth
CREDITS_MAX_ROWS = 500         # …and one reply stays a sane size either way
# The tags a tagger writes when MB has nothing (Vorbis PERFORMER is the common
# one, per player, as "Name (instrument)"). Every name is one a MusicBrainz
# relation states AND one the tag registry knows, so the fallback covers the
# whole credit table rather than the two tags a Vorbis writer happens to fill.
CREDIT_TAG_ROLES = ("PERFORMER", "COMPOSER", "LYRICIST", "WRITER", "ARRANGER",
                    "CONDUCTOR", "REMIXER", "ENGINEER", "PRODUCER", "MIXER",
                    "DJMIXER", "DIRECTOR")


# --------------------------------------------------------------------------- #
# The identity block: everything the panel's header may print
# --------------------------------------------------------------------------- #
# One row per identity FIELD and the canonical tag it reads. The tag names are
# the registry's own (`server/tags_registry.py`, which derives them from
# `mlo.audio.TAG_MAP` — the same keys `AudioFile.all_tags` hands back): this
# module decides what each field MEANS, the registry owns what its tag is
# called, and `_unknown_identity_tags` below fails loudly if a name moves
# instead of printing an empty header.
IDENTITY_TAG_FIELDS = (
    ("title", "TITLE"),
    ("artist", "ARTIST"),
    ("album", "ALBUM"),
    ("album_artist", "ALBUMARTIST"),
    ("catalog_number", "CATALOGNUMBER"),
    ("label", "LABEL"),
    ("barcode", "BARCODE"),
    ("date", "DATE"),
    ("original_date", "ORIGINALDATE"),
    ("country", "RELEASECOUNTRY"),
    ("release_type", "RELEASETYPE"),
    ("media", "MEDIA"),
    ("track_mbid", "MUSICBRAINZ_TRACKID"),
    ("release_mbid", "MUSICBRAINZ_ALBUMID"),
    ("release_group_mbid", "MUSICBRAINZ_RELEASEGROUPID"),
    ("artist_mbid", "MUSICBRAINZ_ARTISTID"),
)
# The block's whole key set: the fields above plus the two that read no tag of
# their own — `recording_mbid` is MusicBrainz's name for the id the
# MUSICBRAINZ_TRACKID tag holds, `path` is the file/folder the panel was
# opened on. EVERY key is always present ("" where nothing states it), so the
# header reads the block without a null check.
IDENTITY_FIELDS = tuple(field for field, _ in IDENTITY_TAG_FIELDS) + (
    "recording_mbid", "path",
)
# The registry is the source of truth for a tag's SPELLING, so a name it does
# not know is a typo — caught here at import, the same loud rule
# tags_registry._assert_known applies to a renamed check.
_unknown_identity_tags = sorted(
    ({tag for _, tag in IDENTITY_TAG_FIELDS} | set(CREDIT_TAG_ROLES))
    - set(_tags_registry.TAG_FAMILY))
if _unknown_identity_tags:
    raise ValueError("integrations: unknown credit tag name(s) "
                     f"{_unknown_identity_tags} — not in server.tags_registry")


def _credit_rows(relations, rows=None):
    """Append one row per MusicBrainz relation to *rows*:
    ``{role, attributes, artist, mbid}``.

    Every artist-target relation is kept whatever its type — MB reports the
    role itself, and a release's players are split across `performer` (Vorbis
    era) and `instrument` (current schema), so filtering by type would drop
    half of them. A relation pointing at a WORK becomes a `work` row whose
    `artist` is the work's title (its own type, usually "performance", rides
    in `attributes`). AND that work node's OWN relations are appended with it:
    `work-level-rels` inlines them into the very same response, and there
    MusicBrainz states the composer / lyricist / writer — the half of the
    credits a recording's own relations never carry.
    """
    rows = [] if rows is None else rows
    for rel in relations or []:
        if len(rows) >= CREDITS_MAX_ROWS:
            break
        attributes = [str(a).strip() for a in (rel.get("attributes") or [])
                      if str(a).strip()]
        if rel.get("target-type") == "work":
            work = rel.get("work") or {}
            title = str(work.get("title") or "").strip()
            if title:
                rows.append({"role": "work", "attributes": attributes or
                             [str(rel.get("type") or "").strip()],
                             "artist": title, "mbid": work.get("id") or ""})
            _credit_rows(work.get("relations"), rows)
            continue
        artist = rel.get("artist") or {}
        name = str(artist.get("name") or rel.get("target-credit") or "").strip()
        if not name:
            continue
        rows.append({"role": str(rel.get("type") or "credit").lower(),
                     "attributes": attributes, "artist": name,
                     "mbid": artist.get("id") or ""})
    return rows


def tidy_credit_rows(rows):
    """Normalised, de-duplicated, role-grouped credit rows — what the UI renders.

    Normalising is ALL this does: a role is lower-cased and trimmed, an empty
    name is dropped (a row nobody is credited on says nothing), and a row
    already seen — same role, person, id and attributes — is kept once, because
    an album repeats the same player on every track and the tag fallback
    repeats the same tag on every file. Sorting by role groups the list the way
    it is displayed. NO role and NO attribute is ever filtered out: MusicBrainz
    states the vocabulary and the panel groups by whatever arrives, so a
    curated subset here would be a credit the user cannot see.
    """
    seen = set()
    out = []
    for row in rows or []:
        role = str(row.get("role") or "").strip().lower()
        artist = str(row.get("artist") or "").strip()
        if not role or not artist:
            continue
        attributes = [str(a).strip() for a in row.get("attributes") or []
                      if str(a).strip()]
        mbid = str(row.get("mbid") or "").strip()
        key = (role, artist.lower(), mbid, tuple(attributes))
        if key in seen:
            continue
        seen.add(key)
        out.append({"role": role, "attributes": attributes, "artist": artist,
                    "mbid": mbid})
    out.sort(key=lambda r: (r["role"], r["artist"].lower()))
    return out


def identity_from_tags(tags, path=""):
    """The `identity` block `/api/credits` answers with: what the panel's
    header may print, read off the files' own tags.

    `tags` is ONE tag dump (a track) or a LIST of them (an album's files) — the
    callers hand over exactly what `tagcache.read_track` already returned, so
    the header costs no extra read and no extra request. The FIRST non-empty
    value wins per field, so a fact only some files carry (a CATALOGNUMBER on
    one track, a RELEASECOUNTRY on another) still prints for the whole release.
    Every field is present — "" where nothing states it — and `path` is the
    file or folder the panel was opened on: the raw path the header prints LAST
    and dimmed, never as the heading it used to be.
    """
    sets = tags if isinstance(tags, (list, tuple)) else [tags]
    by_key = [{str(k).strip().upper(): v for k, v in (t or {}).items()}
              if isinstance(t, dict) else {} for t in sets]

    def value(tag):
        for dump in by_key:
            v = dump.get(tag)
            text = (v if isinstance(v, str) else str(v or "")).strip()
            if text:
                return text
        return ""

    out = {field: "" for field in IDENTITY_FIELDS}
    for field, tag in IDENTITY_TAG_FIELDS:
        raw = value(tag)
        # An id field holds a MusicBrainz id and nothing else: some taggers
        # store a musicbrainz.org URL, which is the same id, and the header is
        # meant to copy and link what MusicBrainz itself uses (integrations.
        # _mbid is the one reader of both shapes).
        out[field] = (_mbid(raw) or "") if field.endswith("_mbid") else raw
    # MusicBrainz calls the id the MUSICBRAINZ_TRACKID tag holds a RECORDING
    # id, so both fields of the block answer from that one tag.
    out["recording_mbid"] = out["track_mbid"]
    # The header names ONE artist: the file's own ARTIST, or the album artist
    # when that is all it states (the same fallback the route's legacy `artist`
    # field makes), with one artist id to go beside it.
    out["artist"] = out["artist"] or out["album_artist"]
    out["artist_mbid"] = (out["artist_mbid"]
                          or _mbid(value("MUSICBRAINZ_ALBUMARTISTID")) or "")
    # Payload paths are forward-slashed everywhere else in the app (the library
    # tree, the album page), and this one is drawn on screen.
    out["path"] = str(path or "").replace("\\", "/")
    return out


def credit_rows_from_tags(tags):
    """Credit rows from a file's OWN tags, for when MusicBrainz has nothing.

    `tags` is a raw tag dump (mlo.audio all_tags, keys already canonicalised to
    PERFORMER/COMPOSER/… when the container maps them). Vorbis writes
    `PERFORMER=Name (instrument)` once per player and some taggers join several
    names with "; ", so both shapes are split back apart. Every tag in
    CREDIT_TAG_ROLES is read — the whole credit table, not just the performers,
    so a file that names its engineer and its mixer shows them too. ponytail: a
    multi-value Vorbis tag collapses to its first value in all_tags — enough
    for a fallback, read af.audio.tags directly if every player must show.
    """
    rows = []
    if not isinstance(tags, dict):
        return rows
    by_key = {str(k).strip().upper(): v for k, v in tags.items()}
    for role in CREDIT_TAG_ROLES:
        for part in re.split(r"\s*;\s*", str(by_key.get(role) or "")):
            part = part.strip()
            if not part:
                continue
            m = re.match(r"^(.*?)\s*\(([^()]*)\)$", part)
            name, attributes = ((m.group(1).strip(), [m.group(2).strip()])
                                if m else (part, []))
            if name:
                rows.append({"role": role.lower(), "attributes": attributes,
                             "artist": name, "mbid": ""})
    return tidy_credit_rows(rows)


def recording_credits(recording_mbid):
    """Credit rows for ONE recording — its players AND its work's songwriters.

    Two relation families from the one request: the recording's own artist
    relations (players, producer, engineer, mix…), and the relations of the
    work the recording performs (`work-rels` names it, `work-level-rels` inlines
    its composer / lyricist / writer). Raises MusicBrainzError (or httpx's own
    error) when MB cannot answer — the caller reports that reason rather than
    an empty result.
    """
    data = mb_get_cached(f"recording/{recording_mbid}",
                         {"inc": "artist-rels+work-rels+work-level-rels",
                          "fmt": "json"})
    return tidy_credit_rows(_credit_rows((data or {}).get("relations")))


def release_credits(release_mbid):
    """Credit rows for a whole RELEASE — every track's recording and work.

    The release request carries each track's own relations (`recording-level-
    rels`) AND the relations of every work those recordings perform
    (`work-level-rels`), so this stays ONE request per album and still merges
    three families: the release's own relations, each recording's, and each
    work's. Only the aggregate is capped, not the request count.
    """
    data = mb_get_cached(
        f"release/{release_mbid}",
        {"inc": "recordings+artist-rels+recording-level-rels+work-rels"
                "+work-level-rels", "fmt": "json"})
    rows = _credit_rows((data or {}).get("relations"))
    recordings = 0
    for medium in (data or {}).get("media") or []:
        for track in medium.get("tracks") or []:
            if (recordings >= CREDITS_MAX_RECORDINGS
                    or len(rows) >= CREDITS_MAX_ROWS):
                break
            recordings += 1
            _credit_rows((track.get("recording") or {}).get("relations"), rows)
    return tidy_credit_rows(rows)


def _release_artist_mbids(artist, album, release, cfg):
    """The release's artist MBIDs, resolved from the name when it has none."""
    from server import discovery

    ids = [a.get("mbid") for a in (release or {}).get("artists") or []
           if a.get("mbid")]
    if not ids and artist:
        try:
            resolved = discovery.resolve_artist_mbid(artist)
        except Exception:
            resolved = None
        if resolved:
            ids.append(resolved)
    return ids


def _genre_source_answers(source, artist, album, release, cfg, tracks):
    """One source's answers: {key: {"level", "genres"[, "title"]}}.

    `key` is a track key for a per-track answer, `_ALL_TRACKS` for an
    album/artist-wide one; both may be present and one TIER supplies a track —
    its own row (level `track`) when the source stated anything for that track,
    else the release-wide row, which itself is labelled with the tier it came
    from (`album` for the release GROUP, `artist` only when the group stated
    nothing — see `merge`). Every source is asked at its best available level —
    a per-track source that cannot answer per track answers nothing rather
    than promoting an album guess to a track. Never raises: the chain wraps
    the call, and every network path inside returns "no answer".

    MusicBrainz is the only id handled — the only genre source this build
    knows (`GENRE_SOURCES`). Any other id returns {} (a stale saved list
    entry), never an album-wide guess.
    """
    from server import discovery

    if source == "musicbrainz":
        answers = {}
        release_mbid = str((release or {}).get("id") or "").strip() if release else ""
        nodes = None            # the album browse, read at most once (lazily)

        def album_nodes():
            """``{recording_mbid: node}`` for this RELEASE — one browse, cached.

            Reached only for a track whose recording states no genre in the
            release payload in hand (`track["genres"]` IS that recording's own
            list, carried by the release lookup itself — VERIFIED live, nested
            recordings answer `genres` with the release's `inc=recordings+
            genres` — so an answered track needs no second read of the same
            fact). What the browse adds is the WORK each SILENT recording
            performs (`inc=work-rels` rides along on the browse, see
            `release_recording_index`): its ids arrive for the whole album in
            ONE request where the per-recording route paid an identity request
            each, and only the work's own genres are then asked.

            Through `_genre_cached`, like every other genre read — one browse
            per release per 30 days, and a browse that did not answer is cached
            as the miss it is (the per-track route below stays the fallback).
            """
            nonlocal nodes
            if nodes is None:
                nodes = {}
                if release_mbid:
                    nodes = _genre_cached(
                        "album", f"musicbrainz|release_recordings|{release_mbid}",
                        lambda r=release_mbid: release_recording_index(r) or None) or {}
            return nodes

        for track in tracks:
            # The TRACK tier reads the RECORDING's own genres — the entity the
            # track IS (R360) — and the WORK's only when the recording states
            # none. The work is the composition, classified once for every
            # recording of it, which is why it is a fallback and not the lead:
            # MEASURED, live, on The Dark Side of the Moon (2026-10-01), the
            # work stated genres for 0 of 4 tracks while the recordings stated
            # 5-9 each, so leading with the work both lost the recording's own
            # answer and paid two rate-limited requests per track for a tier
            # that never spoke. Asking it only when the recording is silent
            # costs nothing on an answered track and still catches the
            # composition's classification when the recording has none.
            recording = str(track.get("recording_mbid") or "").strip()
            names = list(track.get("genres") or [])
            node = None
            if not names and recording:
                # The recording is silent here: the album's browse is the read
                # that carries this recording's own genres (when the release
                # payload was the reduced one) AND the work id it performs.
                node = album_nodes().get(recording)
                if node is not None:
                    names = _genres(node)
            if not names and recording:
                work = _recording_work_id(node) if node is not None else ""
                if work:
                    # The browse stated the work: its genres are ONE cached
                    # request, with no second identity lookup to find it.
                    names = _genre_cached(
                        "recording", f"musicbrainz|work_genres|{work}",
                        lambda w=work: work_genres(w)) or []
                else:
                    # No browse answer (or no release id): the per-recording
                    # route, exactly as it was — the work is found by this
                    # recording's own identity read.
                    names = _genre_cached(
                        "recording", f"musicbrainz|work_genres|{recording}",
                        lambda r=recording: recording_work_genres(r)) or []
            row = _genre_row("track", names)
            if row:
                answers[_genre_track_key(track.get("disc"), track.get("position"))] = row
        # The ALBUM tier is the release GROUP's own genres, and only the
        # group's. The album IS the group, not one pressing (the owner's rule,
        # 2026-10-12), and the numbers say the same: MEASURED live on The Dark
        # Side of the Moon, the release group states 12 genres (ambient, art
        # rock, classic rock, …) while the release states 1 ("classic rock").
        # The release's own names are therefore NOT read — an edition's
        # straggler vote is not the album's classification.
        wide, level = [], "album"
        if release:
            rg = release.get("release_group_id")
            if rg:
                wide += _genre_cached("album", f"musicbrainz|release_group|{rg}",
                                      lambda r=rg: release_group_genres(r)) or []
        else:
            # No release in hand: the release group is resolved from the names.
            rg = None
            try:
                rg = discovery.resolve_release_group(artist, album, cfg)
            except Exception:
                rg = None
            if rg and rg.get("mbid"):
                wide += _genre_cached("album", f"musicbrainz|release_group|{rg['mbid']}",
                                      lambda m=rg["mbid"]: release_group_genres(m)) or []
        # …and the ARTIST tier answers only when the group stated nothing: the
        # artist's classification is the last resort, never a top-up beside the
        # album's own. Which tier the row IS has to be said by where its names
        # came from rather than assumed: an artist's genre labelled `album`
        # would be reported as something it is not AND would outrank the album
        # tier of a later source in the chain (`merge` ranks by this label).
        if not wide:
            level = "artist"
            for mbid in _release_artist_mbids(artist, album, release, cfg):
                wide = _genre_cached(
                    "album", f"musicbrainz|artist|{mbid}",
                    lambda m=mbid: artist_genres(m)) or []
                break
        row = _genre_row(level, wide)
        if row:
            answers[_ALL_TRACKS] = row
        return answers

    return {}


def _answer_by_title(answers, title):
    """The per-track answer whose own title is ours (the fallback mapping).

    A source's per-track rows carry their own titles; when their numbering
    does not line up with ours the title is what maps them. An unmatched title
    yields None — never a neighbouring track's genres.
    """
    want = _norm_compare(title)
    if not want:
        return None
    for key, row in answers.items():
        if key == _ALL_TRACKS or not row.get("title"):
            continue
        if _norm_compare(row["title"]) == want:
            return row
    return None


def _genre_ai_rank(cfg, artist, album, title, candidates, count, extra=None):
    """The AI's take on a track's genres, canonicalized — or None.

    `server.genre_ai.infer_genres` behind the two gates that make this step
    invisible when it is off (`ai_genre_inference`, and an endpoint actually
    configured), and behind a blanket except: a model that times out, refuses
    or answers junk must leave the source list EXACTLY as it was, because the
    import it feeds has to finish either way. None means "no opinion".

    Imported lazily, like the engine imports above: `server.ai` imports THIS
    module for the User-Agent, so a module-level import would be a cycle.

    The answer goes through `mlo.genres.normalize_genres` before anyone sees
    it: the model named the SPECIFIC genres, and the normalizer resolves each
    to MusicBrainz's own spelling and keeps the MODEL's order (it answered the
    specific genres; the cap takes its top N), so the list this returns is the
    one the writers store and the grader reads.
    """
    if not (cfg or {}).get("ai_genre_inference"):
        return None
    try:
        from server import ai as ai_mod
        if not ai_mod.ai_configured(cfg):
            return None
        from server.genre_ai import infer_genres
        from mlo.genres import normalize_genres
        names = infer_genres(artist=artist, album=album, title=title or "",
                             candidates=candidates, count=count, extra=extra)
        return normalize_genres(names, count) if names else None
    except Exception:
        return None


def genre_chain(artist="", album="", release=None, limit=None, sources=None,
                cfg=None, files=None, progress=None):
    """Per-track genres for a release — the stored names, then the aliases.

    The sources are asked under the release's OWN names first (see
    `_genre_chain_names`, which is all of the work). Only when NOTHING answered
    for them — no genre and no per-track row — are the names the FILES carry as
    aliases tried, one substitution at a time, stopping at the first that
    answers.

    WHY: a release whose title is published in another language carries
    `ALBUMALIAS` / `ARTISTALIAS` on its files (`mlo.autotag` writes them), and a
    source that files the release under that name answers only to it. The alias
    pass is therefore free for the ordinary album — it runs only after a pass
    that found nothing — and it never replaces an answer: the stored names'
    result is what is returned when it produced anything at all.
    """
    args = dict(release=release, limit=limit, sources=sources, cfg=cfg,
                files=files, progress=progress)
    got = _genre_chain_names(artist, album, **args)
    if got and (got.get("genres") or got.get("per_track")):
        return got
    for a_name, al_name in _genre_alias_pairs(artist, album, files, cfg):
        alt = _genre_chain_names(a_name, al_name, **args)
        if alt and (alt.get("genres") or alt.get("per_track")):
            return alt
    return got


def _genre_alias_pairs(artist, album, files, cfg):
    """[(artist, album)] substitutions from the FILES' own alias tags, or [].

    The shared walk `mlo.lyrics_providers._alias_queries` does the ordering
    (the whole name localised first, then each entity on its own, capped), so a
    genre lookup and a lyrics lookup substitute names the same way. Nothing is
    read when the files name no alias, and an unreadable file just contributes
    nothing.
    """
    tags = {}
    for path in (files or [])[:1]:
        try:
            from mlo.audio import AudioFile, file_aliases
            tags = file_aliases(AudioFile(path).all_tags())
        except Exception:
            tags = {}
        break
    if not tags:
        return []
    try:
        from mlo.lyrics_providers import _alias_queries
    except Exception:
        return []
    out, seen = [], set()
    for a_name, _t, al_name, _entity, _query in _alias_queries(
            artist, "", album, tags):
        pair = (str(a_name or "").strip(), str(al_name or "").strip())
        if pair != (str(artist or "").strip(), str(album or "").strip()) \
                and pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def _genre_chain_names(artist="", album="", release=None, limit=None,
                        sources=None, cfg=None, files=None, progress=None):
    """Per-track genres for a release, merged from the configured sources.

    The sources are asked IN ORDER until every track is full, and no further:
    the default order (`GENRE_SOURCES`, and the shipped
    `mlo.config.DEFAULT_CONFIG["genre_sources"]`) is MusicBrainz alone.
    A track's genres come from ONE TIER — the recording's own (its WORK only
    when the recording states none), else the release GROUP's, else the
    ARTIST's (see `merge`) — and the release's own (edition) genres are never
    read: the album IS its group, not one pressing. With `ai_genre_inference`
    on and an endpoint configured, the model is the LAST RESORT once the
    sources have not settled a track — see `_genre_ai_rank`, whose answer is
    restricted to published genre names, and `_genre_needs_ai`, which never
    hands over a track the recording itself classified. `mlo.config` migrates
    every previously shipped default list onto this one, so an install that
    never chose an order gets it; a customised list is honoured as written.

    "Full" is the WRITER's own policy applied to the merged list
    (`_genre_complete`): at `mb_genre_count = 2` the track's answer is two
    specific names (or one, from a source that answered at TRACK level — see
    `tracks_full`), so the sources below the one that supplied it are never
    asked —
    `stopped_after` names the source that
    filled the release, and `asked` lists what was actually consulted. A
    source that cannot answer (an id this build does not know) is skipped
    BEFORE any request and reported by name in `skipped`.

    `notes` carries one sentence per source that could not be used; a source
    that DID answer never gets a note.

    Every source that answers contributes; the merged list is deduped
    case-insensitively and capped at `limit` (default `mb_genre_count` from
    Settings → Import, whose shipped value lives in
    `mlo.config.DEFAULT_CONFIG`) **per track**. The names keep the spelling
    their source used — this is the merge point, not a writer: the tag writers
    canonicalize through `mlo.genres.normalize_genres` (which resolves each
    name to MusicBrainz's own and keeps the source's own rank order
    order).

    Each track's own row is tried first, then the release-wide one — and the
    higher tier's answer IS the list: a level below one that answered is a
    fallback, never a top-up, so no stored list blends a recording's names
    with the album's or the artist's. A source that only knows album- or
    artist-wide answers is marked `level: "album"`/`"artist"` in the
    provenance, never `track` — nothing pretends to be per-track.
    `level_counts` totals those tiers over the release's files, and a track
    whose `levels` entry is not `track` was answered by its RELEASE GROUP (or,
    when the group stated nothing, its ARTIST) — which, with two slots, is
    often the whole answer.

    `files` (optional) are the album's audio files; they key the provenance
    maps by path (`sources`, `levels`), which is what the UI shows. A source
    that cannot answer contributes nothing and is reported in `notes` — it
    is never filled in from a guess.

    `progress` (optional) is called as ``progress(i, total, source)`` before
    each source is asked, so a caller can show which source the chain is
    waiting on; a hook that raises is ignored.

    With `ai_genre_inference` on AND an AI endpoint configured, the model is a
    LAST RESORT rather than the first move: it is handed the merged list only
    when the sources did not settle the track themselves (`_genre_needs_ai` —
    the list does not fill the cap, or it was answered below the track's own
    tier) — and a usable answer REPLACES the merged list. The merged list is
    the SPECIFIC names the tier that answered stated, capped at `limit`. The
    answer is
    canonicalized through `mlo.genres.normalize_genres`, the disk cache/gates
    are unchanged, and its path gains "ai" in `sources`. With the setting off,
    no endpoint, or an unusable answer, every field is exactly what the
    sources alone produced.

    Returns {"genres": [...], "per_track": {(disc, position): [...]},
    "per_track_sources": {...}, "per_track_levels": {...},
    "per_track_trimmed": {(disc, position): [name, ...]},
    "sources": {path: [source, ...]}, "levels": {path: "track"|"album"|
    "artist"}, "level_counts": {tier: n}, "per_source": {source: [...]},
    "per_source_counts": {source: {"names": n, "tracks": n}}, "asked":
    [source, ...], "order": [source, ...] (the configured list, in that
    order), "stopped_after": source|None, "skipped": {source: reason},
    "trimmed": [name, ...], "notes": {source: reason}}.
    """
    if cfg is None:
        try:
            from mlo.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    order = [str(s).strip().lower()
             for s in (sources if sources is not None
                       else (cfg.get("genre_sources") or GENRE_SOURCES))
             if str(s).strip()]
    if limit is None:
        from mlo.config import DEFAULT_CONFIG
        try:
            # The shipped default has ONE home: a literal here drifts the
            # moment `mb_genre_count` changes (it is Settings → Import's value).
            limit = max(1, int(cfg.get("mb_genre_count")
                               or DEFAULT_CONFIG["mb_genre_count"]))
        except (TypeError, ValueError):
            limit = DEFAULT_CONFIG["mb_genre_count"]
    artist = str(artist or "").strip()
    album = str(album or "").strip()
    tracks = list((release or {}).get("media") or [])

    # The merge comes FIRST, before any source is asked: it is what decides
    # when the sources can stop being asked (`tracks_full` below), and it is
    # the same function the answer is built with afterwards.
    def merge(track):
        """(genres, sources, level, dropped) in ask order.

        `track=None` is the release-wide answer (every row every source has,
        the release-wide ones first — a union, for the files an album carries
        no track identity for). For a track it is that track's own answer, then
        the release-wide one — and ONE TIER SUPPLIES IT: of the levels that
        stated anything (`track` — the recording, or its work when the
        recording is silent — else `album` — the release GROUP — else
        `artist`), the first one's names are the whole list, capped. A level
        below one that answered is a fallback, never a top-up, so a stored list
        never blends tiers (the owner's rule, 2026-10-12). `dropped` is what
        the cap left out, for the report.
        """
        pairs = []
        for source in order:
            answers = answers_by_source.get(source) or {}
            if track is None:
                rows = [answers[key] for key in
                        sorted(answers, key=lambda k: (k != _ALL_TRACKS, k))]
            else:
                rows = []
                key = _genre_track_key(track.get("disc"), track.get("position"))
                row = answers.get(key) or _answer_by_title(answers, track.get("title"))
                if row:
                    rows.append(row)
                wide = answers.get(_ALL_TRACKS)
                if wide and wide is not row:
                    rows.append(wide)
            for row in rows:
                for name in row.get("genres") or []:
                    pairs.append((str(name).strip(), source,
                                  row.get("level") or "album"))
        kept, seen = [], set()
        for name, source, level in pairs:
            low = name.lower()
            if not name or low in seen:
                continue
            seen.add(low)
            kept.append((name, source, level))
        if track is not None:
            best = min((_LEVEL_RANK.get(lv, 9) for _n, _s, lv in kept), default=9)
            kept = [p for p in kept if _LEVEL_RANK.get(p[2], 9) == best]
        dropped = [n for n, _s, _l in kept[limit:]]
        kept = kept[:limit]
        level = None
        for _name, _source, got in kept:
            if level is None or _LEVEL_RANK.get(got, 9) < _LEVEL_RANK.get(level, 9):
                level = got
        return ([n for n, _s, _l in kept],
                list(dict.fromkeys(s for _n, s, _l in kept)), level, dropped)

    def tracks_full():
        """Whether every track already holds what the writer would write.

        This is the early stop. At `mb_genre_count = 2` the track's answer is
        two specific names (or one, from a source that answered at TRACK level
        — `settled` below), so the sources below the one that supplied it are
        never asked —
        a source that would only repeat the answer must not be paid a request
        for it. Every track
        has to be full: a release whose tracks are answered unevenly keeps
        going for the sake of the ones still empty. A release with no track
        identity at all (the Auto-tagging hook) is judged on its album-wide
        answer instead.
        """
        if not tracks:
            return _genre_complete(merge(None)[0], limit)

        def settled(track):
            """One track: the cap is filled, OR a source answered ITS OWN page.

            A track-level answer is the strongest thing any source can say
            about a track, so it ends the search by itself — the cap is a
            maximum, not a quota (spec R37: one good genre is a complete
            answer). Album- and artist-level answers do NOT: they are the
            release's classification, and stopping on one is what kept the
            per-track tiers below from ever being asked.
            """
            genres, _sources, level, _dropped = merge(track)
            return _genre_complete(genres, limit) or level == "track"

        return all(settled(t) for t in tracks)

    answers_by_source, per_source, per_source_counts = {}, {}, {}
    notes, skipped, asked = {}, {}, []
    stopped_after = None
    for i, source in enumerate(order, 1):
        skip = _genre_source_skip(source, cfg)
        if skip:
            # A source the helper says cannot be asked is reported by name in
            # the same words — "no data" would hide the reason.
            notes[source] = skipped[source] = skip
            continue
        if progress is not None:
            # "source i/N", for a caller that shows a live bar: a source can
            # spend seconds on the network, and the hook must never be able
            # to break the chain.
            try:
                progress(i, len(order), source)
            except Exception:
                pass
        asked.append(source)
        try:
            answers = _genre_source_answers(source, artist, album, release,
                                            cfg, tracks) or {}
        except Exception as e:
            notes[source] = f"failed: {e}"
            continue
        if not answers:
            notes[source] = "no data"
            continue
        answers_by_source[source] = answers
        # The release-wide answer first, then the per-track ones: this list is
        # the album summary the caller may still want for a release whose
        # tracks carry no identity of their own.
        every = []
        for key in sorted(answers, key=lambda k: (k != _ALL_TRACKS, k)):
            every += answers[key].get("genres") or []
        per_source[source] = _genre_names(every)
        # How much this source actually said: a release-wide row answers every
        # track of the release, a per-track row exactly one.
        wide = len(tracks) if answers.get(_ALL_TRACKS) else 0
        own = len([k for k in answers if k != _ALL_TRACKS])
        per_source_counts[source] = {"names": len(per_source[source]),
                                     "tracks": wide + own}
        if tracks_full():
            stopped_after = source
            break

    per_track, per_track_sources, per_track_levels = {}, {}, {}
    per_track_trimmed = {}
    # What the prompt may know about the release beyond its genres; empty
    # values are dropped by the prompt builder, so a release with no date or
    # country simply names itself.
    extra = {"year": str((release or {}).get("date") or "")[:4],
             "country": (release or {}).get("country") or ""}

    for track in tracks:
        genres, contributors, level, dropped = merge(track)
        # The sources are the candidates (best first) and the model ranks
        # them into the hierarchy; a track the model has nothing to say about
        # keeps the merged order untouched. It is only ASKED when the sources
        # could not settle the track themselves (`_genre_needs_ai`).
        #
        # An EMPTY merge is that case in its purest form and is asked too: the
        # sources answered nothing for this track, so the model is the only
        # move left — and with `ai_genre_research` (on by default) it may name
        # a genre they did not. This used to `continue` on an empty list, which
        # made the model unreachable exactly when it was the last resort: a
        # track no source could answer for stayed genre-less with a model
        # configured to look one up.
        ranked = (_genre_ai_rank(cfg, artist, album, track.get("title"),
                                 genres, limit, extra)
                  if _genre_needs_ai(genres, limit, level) else None)
        if ranked:
            genres = ranked
            contributors = list(dict.fromkeys(contributors + ["ai"]))
        if not genres:
            continue
        key = (int(track.get("disc") or 1), int(track.get("position") or 0))
        per_track[key] = genres
        per_track_sources[key] = contributors
        per_track_levels[key] = level
        if dropped:
            per_track_trimmed[key] = dropped

    merged, album_contributors, album_level, album_dropped = merge(None)
    ranked = (_genre_ai_rank(cfg, artist, album, "", merged, limit, extra)
              if _genre_needs_ai(merged, limit) else None)
    if ranked:
        merged = ranked
        album_contributors = list(dict.fromkeys(album_contributors + ["ai"]))
    out_sources, out_levels = {}, {}
    for path in files or []:
        key = None
        try:
            from server import imports
            key = imports._parse_trackno(str(path))
        except Exception:
            key = None
        if key in per_track_sources:
            out_sources[str(path)] = list(per_track_sources[key])
            out_levels[str(path)] = per_track_levels.get(key)
        else:
            # No release identity to key a track on (the Auto-tagging hook has
            # only the file): the release-wide answer is what that file gets,
            # and it says so.
            out_sources[str(path)] = list(album_contributors)
            out_levels[str(path)] = album_level

    # How many tracks each tier ended up answering, for the report: with two
    # slots an ALBUM-level genre is often the whole answer ("shoegaze" +
    # "rock"), and a caller that shows this can say so instead of presenting
    # it as a track's own fact.
    level_counts = {name: 0 for name in _LEVEL_RANK}
    for got in out_levels.values():
        if got in level_counts:
            level_counts[got] += 1

    return {"genres": merged[:limit], "per_track": per_track,
            "per_track_sources": per_track_sources,
            "per_track_levels": per_track_levels,
            "per_track_trimmed": per_track_trimmed,
            "trimmed": album_dropped,
            "sources": out_sources, "levels": out_levels,
            "level_counts": level_counts,
            "per_source": per_source, "per_source_counts": per_source_counts,
            "asked": asked, "order": list(order),
            "stopped_after": stopped_after,
            "skipped": skipped, "notes": notes}


# Fixed genre buckets for GET /api/genres/facets. Every library genre lands in
# exactly one bucket so the UI can offer coarse filters without carrying a full
# taxonomy. Order matters: "folk metal" must count as Metal before Folk sees it.
#
# WHAT the keywords read is the genre's FAMILY first (mlo.genre_vocab.parent_of)
# and the name as spelled only when the vocabulary has no family for it or that
# family matches no bucket here — the two tables are deliberately not the same
# taxonomy, so a name that means one thing and is spelled like another must be
# bucketed by what it IS. The owner's report was a card OTHER holding the chip
# `New Wave 8` while METAL and ROCK bucketed right: the vocabulary knows new wave
# is a rock movement, this keyword list only sees the word "wave", and only the
# spelling was ever consulted. A family with no bucket of its own (latin,
# reggae, blues, industrial — the vocabulary is finer than these nine buckets)
# keeps the Other fallback rather than getting a bucket invented for it.
GENRE_CATEGORIES = [
    ("Metal", ("metal", "doom", "sludge", "djent", "thrash", "grindcore",
               "deathcore", "metalcore", "blackgaze")),
    ("Rock", ("rock", "grunge", "punk", "shoegaze", "psychedelic", "emo",
              "indie", "britpop", "garage", "gothic", "post-punk", "surf")),
    ("Electronic", ("electronic", "techno", "house", "trance", "ambient",
                    "idm", "drum and bass", "dnb", "dubstep", "synth", "electro",
                    "breakbeat", "edm", "downtempo", "dub", "jungle", "glitch")),
    ("Hip-Hop", ("hip hop", "hip-hop", "rap", "trap", "grime", "boom bap")),
    ("Jazz", ("jazz", "bebop", "swing", "fusion", "bossa")),
    ("Classical", ("classical", "baroque", "romantic", "opera", "chamber",
                   "symphony", "orchestral", "choral", "medieval", "renaissance")),
    ("Folk", ("folk", "country", "bluegrass", "celtic", "world", "americana",
              "traditional")),
    ("Soul & Funk", ("soul", "funk", "r&b", "rhythm and blues", "disco",
                     "motown", "gospel")),
    ("Pop", ("pop", "vocal", "ballad", "schlager")),
]
OTHER_CATEGORY = "Other"


def genre_category(name):
    """The fixed bucket a genre name belongs to (never None).

    The name's HEAD is bucketed first (mlo.genre_vocab.top_of — the top of its
    chain, so `deep house` files as Electronic through House), because a genre
    is filed by what it IS rather than by how its spelling happens to read; a
    name the vocabulary cannot place, or a head this table has no keyword for,
    falls back to the keyword match on the name itself. No I/O and nothing
    allocated: one chain walk plus at most two passes over these nine keyword
    tuples per call, and it runs once per distinct library genre.
    """
    text = str(name or "").strip().lower()
    if not text:
        return OTHER_CATEGORY
    for candidate in (_genre_head(name), text):
        if not candidate:
            continue
        for category, keys in GENRE_CATEGORIES:
            if any(k in candidate for k in keys):
                return category
    return OTHER_CATEGORY


def search_releases(query, limit=10, mode="release"):
    """Release search with multiple strategies.

    mode:
      * release  — free-text title/artist search
      * track    — search by track title
      * catno    — search by catalog number  (catno:"CK 62240")
      * barcode  — search by barcode         (barcode:074643924526)
    """
    q = (query or "").strip()
    if not q:
        return []
    if mode == "catno":
        q = f'catno:"{q}"'
    elif mode == "barcode":
        q = f"barcode:{q}"
    elif mode == "track":
        q = f'track:"{q}"'
    try:
        data = mb_get_cached("release", {"query": q, "limit": limit, "fmt": "json"})
        out = []
        for r in data.get("releases", []):
            credit = "".join(
                (ac.get("name", "") + (ac.get("joinphrase", "") or ""))
                for ac in r.get("artist-credit", [])
            )
            label = ""
            for li in r.get("label-info", []) or []:
                if li.get("catalog-number"):
                    label = li["catalog-number"]
                    break
            out.append({
                "id": r.get("id"),
                "title": r.get("title"),
                "date": r.get("date"),
                "artist": credit,
                "country": r.get("country"),
                "status": r.get("status"),
                "catalog_number": label,
                "barcode": r.get("barcode") or "",
                # The search index states each release's text representation
                # too (verified against the live index), so a release picked
                # from a search carries the same language statement the full
                # lookup does — the import's LANGUAGE stamp reads whichever
                # dict it was handed (spec R167).
                "language": str((r.get("text-representation") or {}).get("language") or ""),
                "script": str((r.get("text-representation") or {}).get("script") or ""),
            })
        return out
    except Exception as e:
        return {"error": str(e)}


def search_artists(query, limit=5):
    try:
        data = mb_get_cached("artist", {"query": query, "limit": limit, "fmt": "json"})
        # The search index stores artists' aliases, so the top bar's dropdown
        # can show "稲葉曇 (inabakumori)" without a second request per row (see
        # `alias_for`).
        return [{"id": a.get("id"), "name": a.get("name"), "type": a.get("type"),
                 "alias": alias_for(a.get("aliases"), None, a.get("name"))}
                for a in data.get("artists", [])]
    except Exception as e:
        return {"error": str(e)}


# --------------------------------------------------------------------------- #
# Generic browse: search + artist / release-group / recording pages
# (used by the in-app MusicBrainz browser)
# --------------------------------------------------------------------------- #
MB_ENTITIES = ("artist", "release-group", "release", "recording")

# --------------------------------------------------------------------------- #
# The search field catalogue — what the search box may put in a query
# --------------------------------------------------------------------------- #
# MusicBrainz's own index fields, transcribed from the "Search Fields" tables of
# its API documentation (https://musicbrainz.org/doc/MusicBrainz_API/Search, one
# table per index). NOT invented: every name below is a field that index really
# answers, and every `example` is a value that works against the live index.
# This dict is the ONE source of truth: `search_help()` serves it to the client,
# so the box's completion list and its syntax help cannot drift from what the
# server sends to the index.
#
#   field    the index field, spelled as MusicBrainz spells it in a query
#   kind     text    — free text (a name, a title, a comment)
#            enum    — a closed vocabulary ("Album", "Official", …)
#            date    — a date ("1997-05-21", "1997", "1997-05")
#            number  — a count, a position, milliseconds
#            boolean — "true" / "false"
#            id      — a MusicBrainz ID (a UUID)
#            code    — a fixed-format code (ISO 3166-1 country, 639-3 language,
#                      15924 script, IPI, ISNI, barcode, ASIN, ISRC, ISWC)
#   quotes   whether the value may be quoted: true for `text` and `enum` values,
#            where quoting is what keeps a multi-word value ("Digital Media",
#            "Not applicable") ONE phrase — verified live, `format:Digital Media`
#            drops from 2.8M hits to 25k without quotes — and harmless on a
#            single word. Fixed-format values (dates, ids, codes, numbers,
#            booleans) are compared as-is and are never quoted. Attached below
#            from `_QUOTED_KINDS` rather than spelled out per field: the payload
#            carries the flag, and the rule itself lives in ONE line.
#   meaning  one line: what MusicBrainz matches, in MusicBrainz's own words
#   example  a value to insert after `field:`
MB_SEARCH_FIELDS = {
    "artist": [
        {"field": "alias", "kind": "text", "example": '"Thom Yorke"',
         "meaning": "(part of) any alias attached to the artist (diacritics are ignored)"},
        {"field": "primary_alias", "kind": "text", "example": '"Thom Yorke"',
         "meaning": "(part of) any primary alias attached to the artist (diacritics are ignored)"},
        {"field": "area", "kind": "text", "example": '"United Kingdom"',
         "meaning": "(part of) the name of the artist's main associated area"},
        {"field": "arid", "kind": "id", "example": "9a1bb5ba-1f1e-4f23-a1a4-9ef0e5d39c6c",
         "meaning": "the artist's MBID"},
        {"field": "artist", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the artist's name (diacritics are ignored)"},
        {"field": "artistaccent", "kind": "text", "example": '"Sigur Rós"',
         "meaning": "(part of) the artist's name, diacritics included"},
        {"field": "begin", "kind": "date", "example": "1985-10-01",
         "meaning": "the artist's begin date"},
        {"field": "beginarea", "kind": "text", "example": '"Oxford"',
         "meaning": "(part of) the name of the artist's begin area"},
        {"field": "comment", "kind": "text", "example": '"US progressive rock band"',
         "meaning": "(part of) the artist's disambiguation comment"},
        {"field": "country", "kind": "code", "example": "GB",
         "meaning": "the 2-letter code (ISO 3166-1 alpha-2) for the artist's main associated country"},
        {"field": "end", "kind": "date", "example": "1980-12-08",
         "meaning": "the artist's end date"},
        {"field": "endarea", "kind": "text", "example": '"London"',
         "meaning": "(part of) the name of the artist's end area"},
        {"field": "ended", "kind": "boolean", "example": "true",
         "meaning": "whether the artist has ended (is dissolved or deceased)"},
        {"field": "gender", "kind": "enum", "example": "female",
         "meaning": "the artist's gender (male, female, other or not applicable)"},
        {"field": "ipi", "kind": "code", "example": "00016388225",
         "meaning": "an IPI code associated with the artist"},
        {"field": "isni", "kind": "code", "example": "0000000122809875",
         "meaning": "an ISNI code associated with the artist"},
        {"field": "sortname", "kind": "text", "example": '"Yorke, Thom"',
         "meaning": "(part of) the artist's sort name"},
        {"field": "tag", "kind": "text", "example": '"art rock"',
         "meaning": "(part of) a tag attached to the artist"},
        {"field": "type", "kind": "enum", "example": "Group",
         "meaning": "the artist's type (person, group, orchestra, choir, character, other)"},
    ],
    "release-group": [
        {"field": "alias", "kind": "text", "example": '"OK Computer"',
         "meaning": "(part of) any alias attached to the release group (diacritics are ignored)"},
        {"field": "arid", "kind": "id", "example": "9a1bb5ba-1f1e-4f23-a1a4-9ef0e5d39c6c",
         "meaning": "the MBID of any of the release group artists"},
        {"field": "artist", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the combined credited artist name, join phrases included"},
        {"field": "artistname", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the name of any of the release group artists"},
        {"field": "comment", "kind": "text", "example": '"UK pressing"',
         "meaning": "(part of) the release group's disambiguation comment"},
        {"field": "creditname", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the credited name of any of the release group artists here"},
        {"field": "firstreleasedate", "kind": "date", "example": "1997-05-21",
         "meaning": "the release date of the earliest release in this release group"},
        {"field": "primarytype", "kind": "enum", "example": "Album",
         "meaning": "the release group's primary type (Album, Single, EP, Broadcast, Other)"},
        {"field": "reid", "kind": "id", "example": "1834eae1-741b-3c03-9ca5-0df3decb43ea",
         "meaning": "the MBID of any of the releases in the release group"},
        {"field": "release", "kind": "text", "example": '"OK Computer"',
         "meaning": "(part of) the title of any of the releases in the release group"},
        {"field": "releasegroup", "kind": "text", "example": '"OK Computer"',
         "meaning": "(part of) the release group's title (diacritics are ignored)"},
        {"field": "releasegroupaccent", "kind": "text", "example": '"Ágætis byrjun"',
         "meaning": "(part of) the release group's title, diacritics included"},
        {"field": "releases", "kind": "number", "example": "12",
         "meaning": "the number of releases in the release group"},
        {"field": "rgid", "kind": "id", "example": "b1392450-e666-3926-a536-22c65f834433",
         "meaning": "the release group's MBID"},
        {"field": "secondarytype", "kind": "enum", "example": "Compilation",
         "meaning": "any of the release group's secondary types (Soundtrack, Live, Compilation, "
                    "Remix, Demo, DJ-mix, Mixtape/Street, Field recording, Audio drama, …)"},
        {"field": "status", "kind": "enum", "example": "Official",
         "meaning": "the status of any of the releases in the release group (Official, Promotion, "
                    "Bootleg, Pseudo-Release, Withdrawn, Cancelled)"},
        {"field": "tag", "kind": "text", "example": '"art rock"',
         "meaning": "(part of) a tag attached to the release group"},
        {"field": "type", "kind": "enum", "example": "Album",
         "meaning": "the legacy single release-group type that predates multiple types"},
    ],
    "release": [
        {"field": "alias", "kind": "text", "example": '"OK Computer"',
         "meaning": "(part of) any alias attached to the release (diacritics are ignored)"},
        {"field": "arid", "kind": "id", "example": "9a1bb5ba-1f1e-4f23-a1a4-9ef0e5d39c6c",
         "meaning": "the MBID of any of the release artists"},
        {"field": "artist", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the combined credited artist name, join phrases included"},
        {"field": "artistname", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the name of any of the release artists"},
        {"field": "asin", "kind": "code", "example": "B000002U82",
         "meaning": "an Amazon ASIN for the release"},
        {"field": "barcode", "kind": "code", "example": "724385522925",
         "meaning": "the barcode for the release"},
        {"field": "catno", "kind": "text", "example": '"CDP 7 46001 2"',
         "meaning": "any catalog number for this release (insensitive to case, spaces and separators)"},
        {"field": "comment", "kind": "text", "example": '"UK pressing"',
         "meaning": "(part of) the release's disambiguation comment"},
        {"field": "country", "kind": "code", "example": "GB",
         "meaning": "the 2-letter code (ISO 3166-1 alpha-2) for any country the release was "
                    "released in"},
        {"field": "creditname", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the credited name of any of the release artists on this release"},
        {"field": "date", "kind": "date", "example": "1997-05-21",
         "meaning": "a release date for the release"},
        {"field": "discids", "kind": "number", "example": "2",
         "meaning": "the total number of disc IDs attached to all mediums of the release"},
        {"field": "discidsmedium", "kind": "number", "example": "1",
         "meaning": "the number of disc IDs attached to any one medium of the release"},
        {"field": "format", "kind": "text", "example": '"Digital Media"',
         "meaning": "the format of any medium in the release (insensitive to case, spaces and "
                    "separators)"},
        {"field": "laid", "kind": "id", "example": "df7d1c7f-ef95-425f-8eef-445b3d7bcbd9",
         "meaning": "the MBID of any of the release labels"},
        {"field": "label", "kind": "text", "example": '"Parlophone"',
         "meaning": "(part of) the name of any of the release labels"},
        {"field": "lang", "kind": "code", "example": "eng",
         "meaning": "the ISO 639-3 code for the release language"},
        {"field": "mediumid", "kind": "id", "example": "060959f3-0d81-38b3-be26-c32f80ca68e9",
         "meaning": "the MBID of any of the mediums in the release"},
        {"field": "mediums", "kind": "number", "example": "2",
         "meaning": "the number of mediums in the release"},
        {"field": "packaging", "kind": "text", "example": '"Jewel Case"',
         "meaning": "the packaging of the release (insensitive to case, spaces and separators)"},
        {"field": "primarytype", "kind": "enum", "example": "Album",
         "meaning": "the primary type of the release group for this release"},
        {"field": "quality", "kind": "number", "example": "2",
         "meaning": "the listed data quality of the release (2 for high, 1 for normal)"},
        {"field": "reid", "kind": "id", "example": "1834eae1-741b-3c03-9ca5-0df3decb43ea",
         "meaning": "the release's MBID"},
        {"field": "release", "kind": "text", "example": '"OK Computer"',
         "meaning": "(part of) the release's title (diacritics are ignored)"},
        {"field": "releaseaccent", "kind": "text", "example": '"Ágætis byrjun"',
         "meaning": "(part of) the release's title, diacritics included"},
        {"field": "rgid", "kind": "id", "example": "b1392450-e666-3926-a536-22c65f834433",
         "meaning": "the MBID of the release group for this release"},
        {"field": "script", "kind": "code", "example": "Latn",
         "meaning": "the ISO 15924 code for the release script"},
        {"field": "secondarytype", "kind": "enum", "example": "Compilation",
         "meaning": "any of the release group's secondary types for this release"},
        {"field": "status", "kind": "enum", "example": "Official",
         "meaning": "the status of the release (Official, Promotion, Bootleg, Pseudo-Release, "
                    "Withdrawn, Cancelled)"},
        {"field": "tag", "kind": "text", "example": '"art rock"',
         "meaning": "(part of) a tag attached to the release"},
        {"field": "tracks", "kind": "number", "example": "12",
         "meaning": "the total number of tracks on the release"},
        {"field": "tracksmedium", "kind": "number", "example": "11",
         "meaning": "the number of tracks on any one medium of the release"},
        {"field": "type", "kind": "enum", "example": "Album",
         "meaning": "the legacy single release-group type that predates multiple types"},
    ],
    "recording": [
        {"field": "alias", "kind": "text", "example": '"Airbag"',
         "meaning": "(part of) any alias attached to the recording (diacritics are ignored)"},
        {"field": "arid", "kind": "id", "example": "9a1bb5ba-1f1e-4f23-a1a4-9ef0e5d39c6c",
         "meaning": "the MBID of any of the recording artists"},
        {"field": "artist", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the combined credited artist name, join phrases included"},
        {"field": "artistname", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the name of any of the recording artists"},
        {"field": "comment", "kind": "text", "example": '"live, 1997-08-22: Les Eurockéennes, France"',
         "meaning": "(part of) the recording's disambiguation comment"},
        {"field": "country", "kind": "code", "example": "GB",
         "meaning": "the 2-letter code (ISO 3166-1 alpha-2) for a country any release of this "
                    "recording was released in"},
        {"field": "creditname", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the credited name of any of the recording artists on this recording"},
        {"field": "date", "kind": "date", "example": "1997-05-21",
         "meaning": "the release date of any release including this recording"},
        {"field": "dur", "kind": "number", "example": "284400",
         "meaning": "the recording duration in milliseconds"},
        {"field": "firstreleasedate", "kind": "date", "example": "1997-05-21",
         "meaning": "the release date of the earliest release including this recording"},
        {"field": "format", "kind": "text", "example": '"CD"',
         "meaning": "the format of any medium including this recording (insensitive to case, "
                    "spaces and separators)"},
        {"field": "isrc", "kind": "code", "example": "GBAYE9701274",
         "meaning": "any ISRC associated with the recording"},
        {"field": "number", "kind": "text", "example": '"A4"',
         "meaning": "the free-text number of the track on any medium including this recording"},
        {"field": "position", "kind": "number", "example": "4",
         "meaning": "the position inside its release of any medium including this recording (from 1)"},
        {"field": "primarytype", "kind": "enum", "example": "Album",
         "meaning": "the primary type of any release group including this recording"},
        {"field": "qdur", "kind": "number", "example": "142",
         "meaning": "the recording duration, quantized (duration in milliseconds / 2000)"},
        {"field": "recording", "kind": "text", "example": '"Airbag"',
         "meaning": "(part of) the recording's name, or the name of a track connected to it "
                    "(diacritics are ignored)"},
        {"field": "recordingaccent", "kind": "text", "example": '"Hoppípolla"',
         "meaning": "(part of) the recording's name, diacritics included"},
        {"field": "reid", "kind": "id", "example": "1834eae1-741b-3c03-9ca5-0df3decb43ea",
         "meaning": "the MBID of any release including this recording"},
        {"field": "release", "kind": "text", "example": '"OK Computer"',
         "meaning": "(part of) the name of any release including this recording"},
        {"field": "rgid", "kind": "id", "example": "b1392450-e666-3926-a536-22c65f834433",
         "meaning": "the MBID of any release group including this recording"},
        {"field": "rid", "kind": "id", "example": "4a7fea2e-545b-4c63-bc9a-9943cc3a29d7",
         "meaning": "the recording's MBID"},
        {"field": "secondarytype", "kind": "enum", "example": "Compilation",
         "meaning": "any of the secondary types of any release group including this recording"},
        {"field": "status", "kind": "enum", "example": "Official",
         "meaning": "the status of any release including this recording"},
        {"field": "tag", "kind": "text", "example": '"art rock"',
         "meaning": "(part of) a tag attached to the recording"},
        {"field": "tid", "kind": "id", "example": "ff7733a6-6903-3e2e-b683-6dbbb0f59ec6",
         "meaning": "the MBID of a track connected to this recording"},
        {"field": "tnum", "kind": "number", "example": "4",
         "meaning": "the position of the track on any medium including this recording (from 1, "
                    "pre-gaps at 0)"},
        {"field": "tracks", "kind": "number", "example": "12",
         "meaning": "the number of tracks on any medium including this recording"},
        {"field": "tracksrelease", "kind": "number", "example": "12",
         "meaning": "the number of tracks on any release as a whole including this recording"},
        {"field": "type", "kind": "enum", "example": "Album",
         "meaning": "the legacy single release-group type that predates multiple types"},
        {"field": "video", "kind": "boolean", "example": "false",
         "meaning": "whether the recording is a video recording"},
    ],
    # MusicBrainz documents this index too, so the catalogue covers it; the
    # browser's own search tabs are the four entities above.
    "work": [
        {"field": "alias", "kind": "text", "example": '"Airbag"',
         "meaning": "(part of) any alias attached to the work (diacritics are ignored)"},
        {"field": "arid", "kind": "id", "example": "9a1bb5ba-1f1e-4f23-a1a4-9ef0e5d39c6c",
         "meaning": "the MBID of an artist related to the work (a composer or lyricist)"},
        {"field": "artist", "kind": "text", "example": '"Radiohead"',
         "meaning": "(part of) the name of an artist related to the work (a composer or lyricist)"},
        {"field": "comment", "kind": "text", "example": '"orchestral version"',
         "meaning": "(part of) the work's disambiguation comment"},
        {"field": "iswc", "kind": "code", "example": "T-010.257.770-7",
         "meaning": "any ISWC associated with the work"},
        {"field": "lang", "kind": "code", "example": "eng",
         "meaning": "the ISO 639-3 code for any of the languages of the work's lyrics"},
        {"field": "recording", "kind": "text", "example": '"Airbag"',
         "meaning": "(part of) the title of a recording related to the work"},
        {"field": "recording_count", "kind": "number", "example": "12",
         "meaning": "the number of recordings related to the work"},
        {"field": "rid", "kind": "id", "example": "4a7fea2e-545b-4c63-bc9a-9943cc3a29d7",
         "meaning": "the MBID of a recording related to the work"},
        {"field": "tag", "kind": "text", "example": '"art rock"',
         "meaning": "(part of) a tag attached to the work"},
        {"field": "type", "kind": "enum", "example": "Song",
         "meaning": "the work's type (Song, Opera, Symphony, …)"},
        {"field": "wid", "kind": "id", "example": "6bba3dc1-acc7-3319-84fd-c93883b1049c",
         "meaning": "the work's MBID"},
        {"field": "work", "kind": "text", "example": '"Airbag"',
         "meaning": "(part of) the work's title (diacritics are ignored)"},
        {"field": "workaccent", "kind": "text", "example": '"Hoppípolla"',
         "meaning": "(part of) the work's title, diacritics included"},
    ],
}

_QUOTED_KINDS = ("text", "enum")     # the kinds whose value may be quoted

for _fields in MB_SEARCH_FIELDS.values():
    for _entry in _fields:
        _entry["quotes"] = _entry["kind"] in _QUOTED_KINDS

# Lucene syntax MusicBrainz's search index accepts, as the box's help shows it.
# Each one verified against the live index (tools/test_mb_search.py replays
# them against a stub; the counts below are what the live index answers):
# `artist:"Radiohead" OR artist:"Portishead"` 1,347 releases, `-format:*`
# 240,613, `date:[1990 TO 1999] AND artist:"Radiohead"` 214, `ac\/dc` 1,310
# artists, `"OK Computer"` 54. `form` is what a click inserts into the box.
MB_SEARCH_SYNTAX = [
    {"form": 'artist:"Radiohead"',
     "meaning": "a field and its value — quote a value with spaces so it stays one phrase"},
    {"form": 'artist:"Radiohead" AND releasegroup:"OK Computer"',
     "meaning": "AND narrows (OR widens; NOT or a leading - excludes)"},
    {"form": '"OK Computer"',
     "meaning": "a quoted phrase with no field searches the index's default fields"},
    {"form": "date:[1990 TO 1999]",
     "meaning": "a range, on a date or a number field (release groups: firstreleasedate)"},
    {"form": "-format:*",
     "meaning": "entities with nothing in that field at all"},
    {"form": "ac\\/dc",
     "meaning": "escape a special character with a backslash"},
]


def search_help():
    """The search box's own help: the fields it may use and the syntax to use
    them. Served to the client (`GET /api/mb/search/fields`) from this dict, so
    the completion list and the syntax hint are the same data the server sends
    to the index — a field can never be offered that MusicBrainz would not
    answer."""
    return {"fields": MB_SEARCH_FIELDS, "syntax": MB_SEARCH_SYNTAX}


def _credit(node):
    return "".join(
        (ac.get("name", "") + (ac.get("joinphrase", "") or ""))
        for ac in (node.get("artist-credit") or [])
    )


def _credit_mbid(node):
    """The first credited artist's MBID, or "" — search rows carry the
    artist credit inline, so a row can link to its artist page without a
    second request per row."""
    for ac in node.get("artist-credit") or []:
        aid = ((ac.get("artist") or {}).get("id")) if isinstance(ac, dict) else None
        if aid:
            return aid
    return ""


def _rg_types(node):
    """(primary_type, secondary_types) of a node's release group.

    MusicBrainz embeds the release group as an OBJECT for release SEARCH
    results but as a bare id STRING for browse results (inc=release-groups);
    an id, or anything else unexpected, must read as "type unknown" instead
    of raising AttributeError and failing the whole page."""
    rg = node.get("release-group") if isinstance(node, dict) else None
    if not isinstance(rg, dict):
        return "", []
    return ((rg.get("primary-type") or ""),
            [s for s in (rg.get("secondary-types") or []) if s])


def _isrc(node):
    """The ISRC string of an isrcs entry (an entity wraps it, a recording
    lookup returns it bare)."""
    if isinstance(node, dict):
        return str(node.get("isrc") or "")
    return str(node or "")


def _media_summary(node):
    """'2×CD + DVD' style summary of an entity's media list."""
    parts = []
    for m in node.get("media") or []:
        fmt = m.get("format") or "Unknown"
        if parts and parts[-1][0] == fmt:
            parts[-1][1] += 1
        else:
            parts.append([fmt, 1])
    return " + ".join((f"{n}×{f}" if n > 1 else f) for f, n in parts)


def _release_counts(node):
    """Track/disc numbers for a release: (total, per-disc breakdown).

    The breakdown keeps one number per medium joined with ' + ' — a two-disc
    edition with 10 then 11 tracks reads '10 + 11'; a single disc collapses
    to its plain count."""
    counts = [(m.get("track-count") or 0) for m in node.get("media") or []]
    total = sum(counts)
    breakdown = " + ".join(str(c) for c in counts) if len(counts) > 1 else str(total)
    return total, breakdown


SEARCH_LIMIT_MAX = 100        # WS/2 search refuses limit > 100


def _search_year(field, value):
    """`date:` / `firstreleasedate:` clause for a year or a year range.

    WS/2 indexes these as dates and answers a same-year range with that
    year's entities (verified live: `date:[1999 TO 1999]` and `date:1999`
    return the same count), so one spelling covers a bare year and the
    "1990-1999" range the search box accepts."""
    m = re.fullmatch(r"(\d{4})\s*(?:[-/]\s*(\d{4}))?", str(value or "").strip())
    if not m:
        return ""
    start, end = m.group(1), m.group(2) or m.group(1)
    return f"{field}:[{start} TO {end}]"


def search_query(entity, query="", mode="free", primary_type="", secondary_type="",
                 artist="", year="", label="", catno="", artist_id=""):
    """The Lucene query `search_mb` runs for these inputs.

    Every constraint is its own AND clause, because the index is the only
    place "albums by this artist from 1999 on that label with that catalog
    number" can be answered — filtering the returned rows afterwards cannot
    (the rows that would match are not in the page). Returned to the client
    so the browser can show the user the query behind a result list.
    Field names and value spellings are MusicBrainz's own (release:
    artist/label/catno/date/primarytype/secondarytype/arid; release-group:
    firstreleasedate and no label/catno — a release group has neither).

    WHAT THE APP DOES TO `query`, exhaustively:

      * trims leading/trailing whitespace (a query MusicBrainz would see as
        blank is not one), and
      * ANDs the constraint boxes (and the mode's catno/barcode clause) onto
        it as separate clauses.

    Nothing else: the query is NOT escaped, re-quoted, tokenised or wrapped in
    parentheses, because it is not a phrase to search for but a query the
    index parses — `artist:"Radiohead" AND releasegroup:"OK Computer"` typed
    in the box reaches MusicBrainz byte for byte (bar the trim), quoting and
    operators intact, which is the only way the field syntax the box's help
    teaches can work. Plain text behaves as before: a bare `kid a` is the
    same free-text clause it always was. The constraint values the APP wraps
    are quoted here and nowhere else, and a query that already holds the same
    catno clause does not get a second, identical one.
    """
    clauses = []
    q = str(query or "").strip()
    if entity == "release" and mode == "catno":
        q = f'catno:"{q}"'
    elif entity == "release" and mode == "barcode":
        q = f"barcode:{q}"
    if q:
        clauses.append(q)
    # An explicit Cat # field is a clause of its own; when the mode already
    # turned the same number into one, a second identical clause adds nothing.
    if entity == "release" and catno:
        same_as_query = (mode == "catno"
                         and catno.strip().lower() == str(query or "").strip().lower())
        if not same_as_query:
            clauses.append(f'catno:"{catno.strip()}"')
    if entity == "release" and label:
        clauses.append(f'label:"{label.strip()}"')
    if entity != "artist" and artist:
        clauses.append(f'artist:"{artist.strip()}"')
    if artist_id and entity in ("release-group", "release", "recording"):
        clauses.append(f"arid:{artist_id}")
    if entity in ("release", "release-group", "recording"):
        # a release group carries the earliest release date; releases and
        # recordings carry the date of the release they appear on
        year_clause = _search_year(
            "firstreleasedate" if entity == "release-group" else "date", year)
        if year_clause:
            clauses.append(year_clause)
    if entity in ("release", "release-group"):
        # quoted: several secondary types are multi-word ("Audio drama",
        # "DJ-mix", "Field recording")
        if primary_type:
            clauses.append(f'primarytype:"{primary_type}"')
        if secondary_type:
            clauses.append(f'secondarytype:"{secondary_type}"')
    return " AND ".join(clauses)


def search_mb(entity, query, limit=100, mode="free", offset=0,
              primary_type="", secondary_type="", artist="", year="",
              label="", catno="", artist_id=""):
    """Normalized MB search rows for the four browsable entities.

    mode="free" is the plain full-text search; for releases, mode="catno" /
    "barcode" search by catalog number / barcode (catalog numbers like
    'SRCS 8757' are how pressings are identified). primary_type /
    secondary_type narrow releases and release groups with MusicBrainz's own
    type qualifiers, and artist / year / label / catno are combinable
    constraints (see `search_query`). The request carries ALL of them, so a
    narrow query is answered by the index rather than by throwing rows away.

    Returns {rows, total, offset, next, query}: `total` is MusicBrainz's
    match count, `next` the offset of the following page (None at the end),
    and `query` the Lucene query actually run. Rows keep MusicBrainz's own
    relevance order, and are de-duplicated by MBID — the index repeats an
    entity when a query matches it more than once (a multi-disc release), and
    the same row twice in a table is a bug, not a second result."""
    if entity not in MB_ENTITIES:
        raise ValueError("entity must be artist, release-group, release or recording")
    # WS/2 rejects limit > 100 outright, so a caller asking for more would
    # get an error page instead of a first page: clamp, and let `next` page.
    limit = max(1, min(SEARCH_LIMIT_MAX, int(limit or SEARCH_LIMIT_MAX)))
    offset = max(0, int(offset or 0))
    q = search_query(entity, query, mode, primary_type, secondary_type,
                     artist, year, label, catno, artist_id)
    data = mb_get_cached(entity, {"query": q, "limit": limit, "offset": offset, "fmt": "json"})
    # MB search responses use plural collection keys
    key = {"artist": "artists", "release-group": "release-groups",
           "release": "releases", "recording": "recordings"}[entity]
    raw = [i for i in data.get(key, []) if i.get("id")]
    rows = []
    seen = set()
    for item in raw:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        title = item.get("title") or item.get("name")
        row = {
            "id": item.get("id"),
            "score": item.get("score"),
            "title": title,
            "disambiguation": item.get("disambiguation") or "",
            # The alias the reader's locale names this entity by, when the
            # SEARCH INDEX states one — it does for artists (MusicBrainz stores
            # their aliases in the index) and for nothing else, so a release
            # group or a recording row simply carries "". Those rows show their
            # alias once the page has fetched it (see `alias_for` and the
            # release page's own browse call).
            "alias": alias_for(item.get("aliases"), None, title),
        }
        if entity == "artist":
            area = item.get("area") or {}
            row.update({
                "type": item.get("type") or "",
                "country": area.get("name") or "",
                "life_span": [
                    (item.get("life-span") or {}).get("begin") or "",
                    (item.get("life-span") or {}).get("end") or "",
                ],
                "tags": [t.get("name") for t in (item.get("tags") or [])[:3]],
            })
        elif entity == "release-group":
            row.update({
                "artist": _credit(item),
                "artist_mbid": _credit_mbid(item),
                "primary_type": item.get("primary-type") or "",
                "secondary_types": [s for s in (item.get("secondary-types") or [])],
                "first_release_date": item.get("first-release-date") or "",
            })
        elif entity == "release":
            catalog_number = ""
            for li in item.get("label-info") or []:
                if li.get("catalog-number"):
                    catalog_number = li["catalog-number"]
                    break
            # MB's release search embeds the release group with its PRIMARY
            # type only (the search index carries no secondary types) — that
            # is still what separates an album pressing from a single/EP.
            # `_rg_types` keeps a bare-id embed from crashing the request.
            rg_primary, rg_secondary = _rg_types(item)
            row.update({
                "artist": _credit(item),
                "artist_mbid": _credit_mbid(item),
                "date": item.get("date") or "",
                "country": item.get("country") or "",
                "status": item.get("status") or "",
                "formats": _media_summary(item),
                "track_count": sum((m.get("track-count") or 0) for m in item.get("media") or []),
                "catalog_number": catalog_number,
                "primary_type": rg_primary,
                "secondary_types": rg_secondary,
            })
        else:  # recording
            # The release the recording was pressed on, kept for its COVER:
            # Cover Art Archive answers by release and release group and has no
            # recording endpoint, so a MusicBrainz-only TRACK row's artwork can
            # only come from here (see `discovery._mb_recording_row`). The
            # index repeats a release per matching track, so the first one is
            # MusicBrainz's own ordering, not a pick of ours.
            pressing = (item.get("releases") or [{}])[0]
            row.update({
                "artist": _credit(item),
                "artist_mbid": _credit_mbid(item),
                "length": item.get("length"),
                "first_release_date": item.get("first-release-date") or "",
                "release_mbid": pressing.get("id") or "",
                "release_group_mbid": (pressing.get("release-group") or {}).get("id") or "",
            })
        rows.append(row)
    total = data.get("count") or len(rows)
    # Paging follows the offsets MusicBrainz itself served (the raw slice),
    # never the de-duplicated count, or a dropped duplicate would shift the
    # next page and skip a row.
    nxt = offset + len(raw)
    return {"rows": rows, "total": total, "offset": offset, "query": q,
            "next": nxt if raw and nxt < total else None,
            "duplicates": len(raw) - len(rows)}


def artist_identity(mbid):
    """An artist's identity: name, area, life span, genres, tags.

    Deliberately split from the discography: MusicBrainz answers one request
    per second, so the artist page paints this header while the release
    groups are still being collected."""
    data = mb_get_cached(f"artist/{mbid}",
                         {"inc": "genres+aliases", "fmt": "json"})
    area = data.get("area") or {}
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        # The name in the reader's locale, when MusicBrainz has one (see
        # `alias_for`): the page shows it in parentheses beside `name`.
        "alias": alias_for(data.get("aliases"), None, data.get("name")),
        "disambiguation": data.get("disambiguation") or "",
        "type": data.get("type") or "",
        "country": area.get("name") or "",
        "life_span": [
            (data.get("life-span") or {}).get("begin") or "",
            (data.get("life-span") or {}).get("end") or "",
        ],
        "genres": _display_genres(_genre_names(_genres(data))),
        # Tags ride the same chip row as the genres, so they are capitalized
        # the same way — a row reading "Art Pop · britpop" is the defect this
        # fixes, and a tag is no more a sentence than a genre is.
        "tags": _display_genres(_genre_names(
            [t.get("name") for t in (data.get("tags") or [])[:8]])),
    }


def artist_release_groups(mbid, limit=100, offset=0, primary_type="", secondary_type=""):
    """One page of an artist's release groups, oldest first.

    The discography comes from the *browse* endpoint (release-group?artist=…)
    rather than a lookup's inc= subquery — lookups silently cap the related
    list. It has NO server-side sort, so a single arbitrary 100-row slice
    misrepresents a discography: `_browse_collect` walks the pages (still
    1 req/s) up to `limit` rows so the caller sorts an honest window.

    With a TYPE filter the list comes from the search index instead
    (`arid:` + `primarytype:`/`secondarytype:`): browse cannot filter by type
    at all, and filtering the loaded window is what made an artist page claim
    no albums for an artist whose albums sat beyond the first page. The index
    filters and counts on the server, so the chips and the "N of M" line are
    about the whole discography, not about what happened to be loaded.

    A DERIVED type (mlo.naming.DERIVED_RELEASE_TYPES — "podcast") is the one
    filter the index can NEVER answer: MusicBrainz stores no Podcast release-
    group type to match, so `primarytype:"podcast"` returns nothing by
    MusicBrainz's own design. Such a selection therefore reads the BROWSE
    (which carries `inc=series-rels`, hence each row's own `podcast` block)
    and leaves the selection to the caller's gate — `release_choice.type_matches`
    with the row's derived types. Every row this returns carries `podcast`:
    the series block, or None.
    """
    if (primary_type or secondary_type) and not any(
            is_derived_type(t) for t in (primary_type, secondary_type) if t):
        page = search_mb("release-group", "", limit, "free", offset,
                         primary_type=primary_type, secondary_type=secondary_type,
                         artist_id=mbid)
        return {
            "total": page["total"],
            "offset": offset,
            "next": page["next"],
            "release_groups": sorted(
                (
                    {
                        "id": rg.get("id"),
                        "title": rg.get("title"),
                        "alias": alias_for(rg.get("aliases"), None, rg.get("title")),
                        "primary_type": rg.get("primary_type") or "",
                        "secondary_types": rg.get("secondary_types") or [],
                        "first_release_date": rg.get("first_release_date") or "",
                        # The search index carries no relationships, so a row
                        # from it states no series (the reason a derived-type
                        # filter never takes this branch — above).
                        "podcast": None,
                    }
                    for rg in page["rows"]
                ),
                key=lambda g: g.get("first_release_date") or "9999",
            ),
        }
    # `inc=aliases` on the browse: every row then carries its own aliases, so
    # the alias costs no extra request (the search path above cannot — the
    # search index returns no aliases at all). `series-rels` rides the SAME
    # request: it is what each row's `podcast` block (the series an episode is
    # `part of`) comes from, at no extra MusicBrainz call.
    rgs, total, served = _browse_collect(
        "release-group", {"artist": mbid, "inc": "aliases+series-rels"},
        "release-groups", "release-group-count",
        limit=limit, offset=offset,
    )
    return {
        "total": total,
        "offset": offset,
        "next": (offset + served) if 0 < total and offset + served < total else None,
        "release_groups": [
            {
                "id": rg.get("id"),
                "title": rg.get("title"),
                # This branch reads the BROWSE payload, which carries the
                # aliases (`inc=aliases` above); the search branch above cannot
                # — the index stores aliases for artists only.
                "alias": alias_for(rg.get("aliases"), None, rg.get("title")),
                "primary_type": rg.get("primary-type") or "",
                "secondary_types": rg.get("secondary-types") or [],
                "first_release_date": rg.get("first-release-date") or "",
                # The Podcast series this release group is `part of` (None for
                # everything that is not an episode) — mlo.naming's DERIVED
                # type, read off the browse's own series-rels.
                "podcast": podcast_series_of(rg),
            }
            for rg in sorted(
                rgs,
                key=lambda g: g.get("first-release-date") or "9999",
            )
        ],
    }


def artist_browse(mbid, limit=300, offset=0, primary_type="", secondary_type=""):
    """Identity + a page of release groups together — the auto-import path
    wants both at once, the artist page does not (see artist_identity). The
    type filter is the page's own (see artist_release_groups)."""
    return {**artist_identity(mbid),
            **artist_release_groups(mbid, limit, offset, primary_type, secondary_type)}


def _release_cfg(cfg=None):
    """The config the release-choice policy reads, safe defaults on a CLI.

    `mlo.release_choice` ranks the payloads it is handed against a config
    dict and nothing else, so the ONE config load the policy ever needs lives
    here, next to the MusicBrainz access that consumes the ranking.
    """
    if isinstance(cfg, dict):
        return cfg
    from mlo.config import load_config
    try:
        return load_config()
    except Exception:
        return {}


def _release_group_node(release_group):
    """The release-group fields `mlo.release_choice` reads.

    An absent page (a caller that only holds editions) reads as "no group
    facts": the policy then measures editions against the fullest one offered
    and takes the earliest edition as the original.
    """
    node = release_group if isinstance(release_group, dict) else {}
    return {
        "title": node.get("title"),
        "first_release_date": node.get("first_release_date"),
        "primary_type": node.get("primary_type"),
        "secondary_types": node.get("secondary_types"),
        "track_count": node.get("track_count"),
    }


def ranked_releases(release_group, releases, cfg=None, **kw):
    """`mlo.release_choice.rank_releases` against this app's config.

    The ONE entry point the server uses, so the policy module never sees a
    None config and the "which key, which default" question is answered in
    mlo.config alone.
    """
    return release_choice.rank_releases(_release_group_node(release_group),
                                        releases, _release_cfg(cfg), **kw)


def _best_release(release_group, releases, cfg=None, **kw):
    """(release payload, candidate) for the policy's pick, or (None, None)."""
    rows = list(releases or [])
    ranked = ranked_releases(release_group, rows, cfg, strict=True, **kw)
    best = release_choice.pick(ranked, strict=True)
    return (rows[best.index], best) if best else (None, None)


def pick_releases(releases, cfg=None):
    """The ELIGIBLE releases of *releases*, best first, as the payloads they
    came in as.

    Strict mode of the one release-choice policy (`mlo.release_choice`): a
    promotional/bootleg/pseudo edition is dropped while
    `auto_import_avoid_promo` is on, and an edition with no RELEASECOUNTRY
    while `auto_import_require_country` is on, so no unattended download can
    queue one. The ranking itself — status, medium, track count, date,
    original-vs-reissue, plain title, preferred country — lives there and
    nowhere else (this used to be `release_choice_key`/`release_medium_rank`/
    `_date_rank` here: one policy now).
    """
    rows = list(releases or [])
    ranked = ranked_releases(None, rows, cfg, strict=True)
    return [rows[c.index] for c in ranked if c.eligible and c.type_ok]


def pick_release(releases, cfg=None):
    """The single best release of a group per `pick_releases`, or None."""
    kept = pick_releases(releases, cfg)
    return kept[0] if kept else None


def resolve_release(mbid):
    """(release, release_mbid) for a release id, a release-group id or a URL.

    Auto-import works on a *release* — a concrete pressing with a track list
    — while the ids pasted into an import are usually release GROUPS, and a
    group id passed to the release endpoint 404s. Group ids are resolved to
    their best edition via the release-choice policy so no caller (HTTP
    route, bulk import) can queue a group job.
    Returns (None, mbid) when the id is a group with no usable edition, and
    (None, None) when nothing matches at all. A MusicBrainz OUTAGE is not
    "nothing matches" — that raises MusicBrainzError so the caller reports
    "MusicBrainz is busy" instead of claiming the release does not exist."""
    rid = _mbid(mbid)
    if not rid:
        return None, None
    try:
        return release_lookup(rid), rid
    except MusicBrainzError:
        raise
    except Exception:
        pass
    try:
        rg = release_group_browse(rid, limit=100, offset=0)
    except MusicBrainzError:
        raise
    except Exception:
        return None, None
    best, _pick = _best_release(rg, rg.get("releases") or []) if rg.get("id") else (None, None)
    if not best or not best.get("id"):
        return None, rid
    try:
        return release_lookup(best["id"]), best["id"]
    except MusicBrainzError:
        raise
    except Exception:
        return None, rid


_NO_EDITION = ("no edition eligible for auto-import (promotional/bootleg "
               "editions are skipped while auto_import_avoid_promo is on, and "
               "editions with no release country while "
               "auto_import_require_country is on)")
# Release groups one bulk call expands: each costs a MusicBrainz browse
# (1 req/s), so an artist with hundreds of groups would take minutes — the
# remainder is reported as skipped instead of silently dropped or wedging.
# It is the DEFAULT bound, for a call that has to answer (the auto-import
# route's quick attempt); a caller working off the request passes
# `limit=None` and expands every group its own row named (see
# `auto_import_targets`).
BULK_MAX_GROUPS = 50


def no_edition_reason(ranked):
    """Why the strict policy refused every edition of a group, in words.

    The policy's own first eligibility sentence when it has one — so a caller
    (and the watch's `last_error`) reads "no release country …" or "release
    group type is not the type asked for" rather than a generic refusal.
    """
    if not ranked:
        return "MusicBrainz lists no edition of this release group"
    refused = next((c for c in ranked if not c.eligible), None)
    detail = refused.reasons[0] if refused and refused.reasons else ""
    return f"no eligible edition for auto-import ({detail})" if detail else _NO_EDITION


def group_targets(rg_mbid, mode, *, types=None, primary_type="", secondary_type=""):
    """([{mbid,title,score,reasons,candidates}], error) — the releases of a group to queue.

    Ranked by the one release-choice policy in strict mode (see
    `pick_releases`); `mode` "best" keeps only the pick, "all" every eligible
    edition, best first. `types` / `primary_type` / `secondary_type` restrict
    the pick to the release-group type the caller is after — a watch's own
    type filter — so a watch for albums can never queue a single. `error` is a
    readable reason and never an exception, so one unusable group cannot abort
    a whole discography.

    Every row also carries `candidates`: the eligible editions in that same
    order as `{mbid,title,score}`, the row's own `mbid` first (spec R150). That
    is the fallback an acquisition walks when the network does not have the
    edition it started with — "best" therefore truncates the ROWS to one, never
    the ranking."""
    try:
        rg = release_group_browse(rg_mbid, limit=100, offset=0)
    except Exception as e:
        return [], f"MusicBrainz release-group lookup failed: {e}"
    if not rg.get("id"):
        return [], "not a MusicBrainz release group"
    # The caller's type filter gates the GROUP itself, by the ONE rule the
    # choice then applies to its editions: a group of another type is reported
    # with the type it actually is, never answered with an edition of a group
    # that was not asked for.
    if types and not release_choice.type_matches(
            rg.get("primary_type"), rg.get("secondary_types"), types,
            release_choice.derived_types(rg)):
        return [], type_skip_reason(rg.get("primary_type"),
                                    rg.get("secondary_types"))
    ranked = ranked_releases(rg, rg.get("releases") or [], strict=True,
                             wanted_types=types, primary_type=primary_type,
                             secondary_type=secondary_type)
    eligible = [c for c in ranked if c.eligible and c.type_ok]
    if not eligible:
        return [], no_edition_reason(ranked)
    # The FALLBACK list (spec R150), on every row this returns: the eligible
    # editions in the policy's OWN order, best first, so the acquisition of the
    # single album a row creates can move on to the next pressing when the
    # network does not have the first. It costs no request — these are the
    # editions the browse above already returned — and it rides on the row
    # instead of queueing one album per edition, because one release group is
    # one album folder (R142). Every entry names the same fields a row does
    # (`mbid`/`title`/`score`), and the FIRST entry is always the row's own
    # `mbid`.
    #
    # Each entry also carries the edition's OWN facts (date, status, medium,
    # track count, disambiguation comment, country). They are what the walk
    # ranks by when it reads the stored list (`mlo.release_choice.rank_stored`):
    # the ORDER this list is written in is a snapshot of the policy at add time,
    # and the walk re-derives it from these facts with the policy in force then
    # — so a release queued before a rule changed is searched by the new rule.
    fallback = [{"mbid": c.release_mbid, "title": c.title, "score": c.score,
                 "catalog_numbers": list(c.catalog_numbers),
                 "date": c.date, "status": c.status, "country": c.country,
                 "disambiguation": c.disambiguation,
                 "medium_formats": list(c.media),
                 "track_count": int(c.track_count or 0)} for c in eligible]
    # …and the same number twice is the same SEARCH: the editions behind the
    # best one are only worth asking for when they are a DIFFERENT pressing
    # (`distinct_pressings` — the rule and its reasoning live there).
    fallback, _duplicates = release_choice.distinct_pressings(fallback)
    rows = eligible if mode == "all" else eligible[:1]
    out = []
    for c in rows:
        row = {"mbid": c.release_mbid, "title": c.title, "score": c.score,
               "reasons": list(c.reasons)}
        # `mode="all"` queues every eligible edition as its OWN album — the user
        # asked for all of them — so each of those rows is a one-candidate
        # acquisition: there is no "next best" behind an edition that IS the
        # target.
        row["candidates"] = ([{"mbid": c.release_mbid, "title": c.title,
                               "score": c.score}]
                             if mode == "all" else fallback)
        out.append(row)
    return out, None


def type_skip_reason(primary_type, secondary_types):
    """Why a release group is NOT one of the types a caller asked for.

    The group's own type is named the way MusicBrainz spells it ("Album +
    Compilation"), so a skipped row says what the group IS, not only that it
    was left out.
    """
    label = " + ".join([str(primary_type or "").strip()]
                       + [str(s).strip() for s in (secondary_types or [])
                          if str(s).strip()])
    return ("release-group type not requested ("
            + (label or "MusicBrainz states no type") + ")")


def groups_of_types(groups, types):
    """(kept, skipped) — the release-group rows a `types` filter keeps.

    `types` is MusicBrainz's own vocabulary (a combined form like "Album +
    Compilation" included) and every row is judged by the ONE rule in
    `mlo.release_choice.type_matches` — the filter the artist watch and the
    release choice already apply. An EMPTY selection keeps every group: no
    filter means no filter, which is what every caller did before the filter
    existed. Each dropped row comes back as `{mbid, reason}`
    (`type_skip_reason`), so a filtered-out group is REPORTED and never
    silently dropped.
    """
    if not types:
        return list(groups or []), []
    kept, skipped = [], []
    for g in groups or []:
        if release_choice.type_matches(g.get("primary_type"),
                                       g.get("secondary_types"), types,
                                       release_choice.derived_types(g)):
            kept.append(g)
        else:
            skipped.append({"mbid": str(g.get("id") or ""),
                            "reason": type_skip_reason(g.get("primary_type"),
                                                       g.get("secondary_types"))})
    return kept, skipped


def _kind_for(mbid):
    """release / release_group / artist for an ID that did not say which."""
    t = (detect_mbid(mbid) or {}).get("type") or ""
    return {"release-group": "release_group"}.get(t, t) or None


def releases_for_disc_id(disc_id):
    """The releases MusicBrainz holds for a Disc ID — one row per release.

    A Disc ID is a hash of the disc's own track table (mlo.discs), so this is
    the one lookup that answers "WHICH pressing is this?" from the rip log
    alone: no tag, no fingerprint, no name search. `GET /ws/2/discid/<id>`
    answers every release attached to that disc — a compilation, a reissue and
    its original all share one — and each row here carries what an import
    needs to offer one: the release's id, title, artist and date, the MEDIUM
    that holds this disc (its position, format and track count) and the
    tracklist, in order.

    Rows are ordered by the medium's own position then the release's date, so
    a single-disc release (medium 1 of a set that states one disc) is the
    first row — the ordinary case — and a boxed set's later discs keep their
    order. Raises `MusicBrainzError` when the service does not answer; an
    unknown Disc ID is an empty list, not an error."""
    from urllib.parse import quote

    data = mb_get("discid/" + quote(str(disc_id)),
                  {"inc": "artist-credits+recordings+release-groups",
                   "fmt": "json"}, timeout=30.0)
    rows = []
    for rel in (data or {}).get("releases") or []:
        rid = str(rel.get("id") or "")
        if not rid:
            continue
        credits = [c for c in (rel.get("artist-credit") or []) if isinstance(c, dict)]
        artist = "".join(str(c.get("name") or (c.get("artist") or {}).get("name") or "")
                         for c in credits)
        artist_mbid = ""
        for c in credits:
            mbid = str((c.get("artist") or {}).get("id") or "")
            if mbid:
                artist_mbid = mbid
                break
        media = [m for m in (rel.get("media") or []) if isinstance(m, dict)]
        mine = None
        for m in media:
            for d in (m.get("discs") or []):
                if str((d or {}).get("id") or "") == str(disc_id):
                    mine = m
                    break
            if mine is not None:
                break
        tracks = []
        for m in ([mine] if mine is not None else media):
            for t in (m.get("tracks") or []):
                tracks.append({"position": t.get("position"),
                               "title": t.get("title") or t.get("name") or ""})
        rows.append({
            "release_id": rid,
            "title": rel.get("title") or "",
            "artist": artist,
            "artist_mbid": artist_mbid,
            "date": rel.get("date") or "",
            "country": rel.get("country") or "",
            "status": rel.get("status") or "",
            "track_count": int(mine.get("track-count") or 0) if mine else int(rel.get("track-count") or 0),
            "disc_position": int(mine.get("position") or 0) if mine else 0,
            "discs": len(media),
            "format": (mine.get("format") or "") if mine else "",
            "matches_this_disc": mine is not None,
            "tracks": tracks,
        })
    rows.sort(key=lambda r: (r["disc_position"] or 99, r["date"] or "9999"))
    return rows


def auto_import_targets(mbid, kind=None, mode="best", types=None,
                        limit=BULK_MAX_GROUPS):
    """([{mbid,title,candidates}], [{mbid,reason}]) — what a bulk auto-import should queue.

    ONE resolution path, shared by the HTTP route's bounded quick attempt and
    by the auto-import job that redoes the whole thing when that attempt did
    not finish: a release resolves to the edition the policy picks, a release
    group to its best (or every eligible) edition, an artist to one best
    release per release group it does not already own. A MusicBrainz outage
    raises MusicBrainzError — reported per item, never as "does not exist".

    Every row carries `candidates`, the ranked editions it may be acquired
    from, best first (spec R150): the release-choice policy's own order, with
    the row's `mbid` first. A group row and an artist's per-group row carry the
    group's whole eligible list, so the caller that records the request has the
    fallback in hand without a second browse; a row for an explicitly named
    RELEASE carries itself alone, because a named pressing has no next best.

    `types` restricts an ARTIST or release-group request to MusicBrainz's own
    release-group types, matched by `mlo.release_choice.type_matches` — the
    same rule (and the same vocabulary) the artist watch's type filter and
    the release choice use, so "Album + Compilation" selects those groups and
    not the Singles. An EMPTY selection is every type, exactly the behaviour
    every caller had before the filter existed; a group the filter leaves out
    is reported in the second list WITH its reason, never silently dropped.

    `limit` is how many of an artist's release groups one call may EXPAND
    (each costs a MusicBrainz browse, 1 req/s), the default being the bound a
    call that has to answer keeps. `limit=None` expands every group the filter
    keeps: that is what a caller working off the request (the artist page's
    discography prepare, one album appearing at a time) owes the button that
    named the type — with a bound there, the row's own list was truncated and
    the rest came back as "call again" for an action the user had already
    asked for in full.

    A user's own edition choice does NOT come through here: the route resolves
    it (`server.api_add._group_edition_targets`) so the group-membership check
    sits with the request that named both ids.
    """
    mode = "all" if str(mode or "").lower() == "all" else "best"
    kind = str(kind or "auto").strip().lower()
    if kind in ("", "auto"):
        kind = _kind_for(mbid) or "release"
    # "Already in the library" is checked for every kind, not just artists:
    # queuing a release or a group the library already holds downloaded the
    # same album a second time, and the duplicate then landed beside it.
    from mlo.config import load_config
    from server import library as lib_mod
    owned = lib_mod.owned_mbids(load_config())
    if kind == "release":
        rel, rid = resolve_release(mbid)
        if not rid:
            return [], [{"mbid": mbid, "reason": "no MusicBrainz release or "
                                                 "release group matches this ID"}]
        if not rel:
            return [], [{"mbid": rid, "reason": _NO_EDITION}]
        # A NAMED release is the user's OWN pick (the release page's "add this
        # pressing", a manual grab): it is refused only when THAT release is
        # already in the library, never because a sibling edition of the same
        # release group is. The group-level skip belongs to the bulk paths
        # below — a release GROUP's editions or an artist's discography — where
        # an album the library already holds must not be fetched again.
        if rid.lower() in owned:
            return [], [{"mbid": rid, "reason": "already in the library"}]
        # ONE candidate: the caller named THIS pressing, so there is no second
        # best to fall back to — the fallback belongs to a release GROUP, whose
        # editions the policy ranked (spec R150).
        row = {"mbid": rid, "title": rel.get("title") or "",
               "candidates": [{"mbid": rid, "title": rel.get("title") or ""}]}
        return [row], []
    if kind == "release_group":
        if str(_mbid(mbid) or "").lower() in owned:
            return [], [{"mbid": mbid, "reason": "already in the library"}]
        rows, err = group_targets(mbid, mode, types=types)
        return rows, ([{"mbid": mbid, "reason": err}] if err else [])
    if kind != "artist":
        return [], [{"mbid": mbid, "reason": f"unknown MusicBrainz kind {kind!r}"}]

    artist = artist_browse(mbid, limit=500, offset=0)
    groups = artist.get("release_groups") or []
    if not groups:
        return [], [{"mbid": mbid,
                     "reason": "this artist has no release groups on MusicBrainz"}]
    # The type filter is applied to the GROUP LIST first: a group the caller
    # did not ask for is never even browsed (each one costs a MusicBrainz
    # request), and it leaves a skip row naming the type it actually is.
    groups, skipped = groups_of_types(groups, types)
    rows, done = [], 0
    for g in groups:
        gid = str(g.get("id") or "")
        if not gid:
            continue
        if gid.lower() in owned:
            skipped.append({"mbid": gid, "reason": "already in the library"})
            continue
        if limit is not None and done >= limit:
            skipped.append({"mbid": gid,
                            "reason": f"per-call limit of {limit} "
                                      "release groups reached — call again"})
            continue
        sub, err = group_targets(gid, "best", types=types)
        done += 1
        if err:
            skipped.append({"mbid": gid, "reason": err})
            continue
        rows.extend(sub)
    return rows, skipped


def _catalogs_of(rel):
    """The catalog numbers a browse row states, in MusicBrainz's own order.

    One call into `mlo.release_choice` — the same reader the policy fills a
    Candidate from and the same one the walk compares by (R169) — so the page's
    rows, the ranked candidates and the fallback list can never disagree about
    which number an edition carries.
    """
    return release_choice.catalog_numbers(rel or {})


def release_group_browse(mbid, limit=300, offset=0):
    """Release-group page: identity + its releases (editions), each with
    media so every row carries format, disc count and its '10 + 11' track
    breakdown. Releases come from the browse endpoint (collected across
    pages) because the lookup's release subquery both truncates and omits
    media.

    The payload also carries `countries`: every (country, date) the group's
    editions were released in, each stamped with the edition that carries it
    (a release carries its own events, so the union costs no extra request).
    The editions of the pages loaded so far are what it covers.
    """
    data = mb_get_cached(
        f"release-group/{mbid}",
        {"inc": "artist-credits+genres+aliases+series-rels", "fmt": "json"},
    )
    # `labels` rides the SAME request (MusicBrainz's browse answers it), and it
    # is what each row's `label-info` — hence every edition's CATALOG NUMBERS —
    # comes from. The fallback walk needs them: separate releases can share one
    # number, and two editions that would run the same catalog-number search
    # are one thing to try (`mlo.release_choice.distinct_pressings`).
    rel_rows, total, served = _browse_collect(
        "release", {"release-group": mbid, "inc": "media+aliases+labels"},
        "releases", "release-count",
        limit=limit, offset=offset,
    )
    # One config load for both readers of it (the ranking and the country
    # chips' preference mark).
    cfg = _release_cfg()
    releases, ranked_rows = [], []
    # The editions are listed in the policy's own order (mlo.release_choice),
    # so the first row is the edition auto-import would take — and the sort is
    # deliberately NOT strict: a promo or a country-less edition still belongs
    # on the page, ranked where the policy puts it.
    for cand in ranked_releases(
            {"title": data.get("title"),
             "first_release_date": data.get("first-release-date"),
             "primary_type": data.get("primary-type"),
             "secondary_types": data.get("secondary-types")},
            rel_rows, cfg):
        r = rel_rows[cand.index]
        ranked_rows.append(r)
        track_count, track_breakdown = _release_counts(r)
        releases.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "alias": alias_for(r.get("aliases"), None, r.get("title")),
            "date": r.get("date") or "",
            "country": r.get("country") or "",
            "status": r.get("status") or "",
            "disambiguation": r.get("disambiguation") or "",
            "medium": r.get("format") or "",
            "formats": _media_summary(r),
            "disc_count": len(r.get("media") or []),
            "track_count": track_count,
            "track_breakdown": track_breakdown,
            "barcode": r.get("barcode") or "",
            # Every catalog number this edition states (`catalog_numbers`) and
            # the first one (`catalog_number`, the key `release_lookup` also
            # carries) — off the browse's own `label-info`, which is why the
            # browse asks for `labels`. The fallback walk reads them to tell two
            # releases that are one SEARCH apart (R169), and the group page can
            # show them beside the rest of the pressing's identity.
            "catalog_numbers": _catalogs_of(r),
            "catalog_number": (_catalogs_of(r) or [""])[0],
            "score": cand.score,
            "reasons": list(cand.reasons),
        })
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "disambiguation": data.get("disambiguation") or "",
        "artist": _credit(data),
        "artist_mbid": next(
            (ac["artist"]["id"] for ac in data.get("artist-credit") or [] if "artist" in ac), None
        ),
        "primary_type": data.get("primary-type") or "",
        "secondary_types": data.get("secondary-types") or [],
        "genres": _display_genres(_genre_names(_genres(data))),
        "first_release_date": data.get("first-release-date") or "",
        # The Podcast series this group is `part of`, or None — MusicBrainz
        # has no Podcast release-group type, so a podcast is only ever this
        # relation (mlo.naming's DERIVED type; the `inc=series-rels` above is
        # what makes it free on this request).
        "podcast": podcast_series_of(data),
        "countries": release_group_countries(
            ranked_rows, cfg.get("prefer_release_country")),
        "total": total,
        "offset": offset,
        "next": (offset + served) if 0 < total and offset + served < total else None,
        "releases": releases,
    }


def recording_browse(mbid, limit=300, offset=0):
    """Recording ('track') page: identity + releases carrying it (browsed,
    with media, for the same reasons as the release-group page)."""
    data = mb_get_cached(
        f"recording/{mbid}",
        {"inc": "artist-credits+isrcs+genres", "fmt": "json"},
    )
    rel_rows, total, served = _browse_collect(
        "release",
        {"recording": mbid, "inc": "media+artist-credits+release-groups"},
        "releases", "release-count",
        limit=limit, offset=offset,
    )
    releases = []
    # Policy order (mlo.release_choice), so the first row is the release
    # "Add to library" would take and a caller that falls back to `rows[0]`
    # falls back to the pick, never to an arbitrary edition. No group payload
    # is in hand here, so the policy's "original" is the earliest release
    # carrying this recording.
    for cand in ranked_releases(None, rel_rows):
        r = rel_rows[cand.index]
        track_count, track_breakdown = _release_counts(r)
        rg_primary, rg_secondary = _rg_types(r)
        releases.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "date": r.get("date") or "",
            "country": r.get("country") or "",
            "status": r.get("status") or "",
            "formats": _media_summary(r),
            "disc_count": len(r.get("media") or []),
            "track_count": track_count,
            "track_breakdown": track_breakdown,
            # the release group's full type: primary (Album/EP/Single/...) plus
            # secondary (Soundtrack/Live/Compilation/...), so a score album
            # reads "Album + Soundtrack" instead of a bare "Album".
            "primary_type": rg_primary,
            "secondary_types": rg_secondary,
        })
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "disambiguation": data.get("disambiguation") or "",
        "artist": _credit(data),
        "artist_mbid": next(
            (ac["artist"]["id"] for ac in data.get("artist-credit") or [] if "artist" in ac), None
        ),
        "length": data.get("length"),
        "genres": _display_genres(_genre_names(_genres(data))),
        # a recording lookup returns bare ISRC strings ("USRC17607839") while
        # some other entities wrap them in {"isrc": ...} — .get() on a string
        # raised AttributeError and 502'd the whole recording page.
        "isrcs": [v for v in (_isrc(i) for i in data.get("isrcs") or []) if v],
        "total": total,
        "offset": offset,
        "next": (offset + served) if 0 < total and offset + served < total else None,
        "releases": releases,
    }


# --------------------------------------------------------------------------- #
# Track matching (local files <-> release media)
# --------------------------------------------------------------------------- #
def match_tracks(local_tracks, release):
    """Suggest release track/disc for each local track.

    local_tracks: list of {path, file, tracknumber, discnumber, title, duration}
    Matching: exact (disc,position) hit first, then title-similarity fallback.
    """
    release_media = release.get("media", [])
    by_pos = {(m["disc"], m["position"]): m for m in release_media}
    # A file that states a disc the release does not have falls back to disc 1
    # ONLY when the release carries a single medium — there, disc 1 is the one
    # place its track can be — and even then at a LOWER score, because the
    # number the file states did not answer. On a multi-disc release the
    # disc-1 fallback is gone: it silently attached a disc-2 file to disc 1's
    # same-numbered track at score 1.0 (a wrong MUSICBRAINZ_TRACKID written to
    # the file, and the wrong recording's ratings/genres behind it), and the
    # title pass below is the honest answer instead.
    single_medium = len({m["disc"] for m in release_media}) <= 1
    suggestions = []
    for lt in local_tracks:
        tn = lt.get("tracknumber")
        dn = lt.get("discnumber") or 1
        match = None
        score = 0.0
        if tn is not None:
            m = by_pos.get((dn, tn))
            if m:
                match, score = m, 1.0
            elif single_medium and dn != 1:
                m = by_pos.get((1, tn))
                if m:
                    match, score = m, 0.5
        if match is None and lt.get("title"):
            best, best_score = None, 0.0
            kind = title_variant_kind(lt["title"])
            for m in release_media:
                # A variant of the track (instrumental/karaoke/…) is another
                # recording: it may never be suggested as this file's track,
                # whatever the name similarity says.
                got = title_variant_kind(m.get("title"))
                if (kind or got) and kind != got:
                    continue
                s = _similarity(lt["title"], m.get("title", ""))
                if s > best_score:
                    best, best_score = m, s
            if best and best_score >= 0.6:
                match, score = best, best_score
        suggestions.append({
            "local": lt.get("path"),
            "file": lt.get("file"),
            "matched": match is not None,
            "confidence": score,
            "release_track": match,
        })
    return suggestions


def _similarity(a, b):
    import difflib
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


# --------------------------------------------------------------------------- #
# LRCLIB
# --------------------------------------------------------------------------- #
def _lrclib_ua():
    """The ONE LRCLIB User-Agent: the engine client's, which carries the app's
    own version (LRCLIB's implementation requirements ask for name, version
    and a link). Imported here rather than spelled twice — this module's own
    `USER_AGENT` is MusicBrainz's and says "2.0", a version the app left
    behind."""
    from mlo.lyrics_providers import USER_AGENT

    return USER_AGENT


_LRCLIB_HEADERS = {"User-Agent": _lrclib_ua()}
_lrclib_last = 0.0
_lrclib_lock = threading.Lock()
# LRCLIB answers are cached on disk (30 days, the app's other provider caches'
# TTL): a library pass asks the same tracks again on every import, and a track
# LRCLIB does not know costs TWO requests (a 404 `/get`, then `/search`) that
# answer the same thing every time. Without this the memory memo
# (`_advisory_cached`) was all there was — it dies with the process.
_LRCLIB_CACHE_TTL = 30 * 86400.0


class _LRCLIBCachedResponse:
    """A cached LRCLIB answer in the shape the callers read: `.status_code`
    and `.json()`, exactly the httpx response's two used attributes."""
    __slots__ = ("status_code", "_body")

    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _lrclib_cache_key(endpoint, params):
    import hashlib
    from urllib.parse import urlencode
    raw = f"{endpoint}?{urlencode(sorted((params or {}).items()))}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _lrclib_cache_read(key):
    d = _data_cache_dir("lrclib_cache")
    if not d:
        return None
    fp = os.path.join(d, key + ".json")
    try:
        if os.path.isfile(fp) and time.time() - os.path.getmtime(fp) < _LRCLIB_CACHE_TTL:
            with open(fp, "r", encoding="utf-8") as fh:
                row = json.loads(fh.read())
            return _LRCLIBCachedResponse(int(row["status"]), row.get("body"))
    except Exception:
        return None
    return None


def _lrclib_cache_write(key, status, body):
    d = _data_cache_dir("lrclib_cache")
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, key + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"status": int(status), "body": body}, fh)
        os.replace(tmp, os.path.join(d, key + ".json"))
    except Exception:
        pass


def _lrclib_get(endpoint, params, timeout=15, retries=3):
    """Rate-throttled LRCLIB GET with retry on 429/5xx (they throttle IPs).

    The answer is served from a 30-day DISK cache first (`lrclib_cache`
    beside the app's other provider caches), so a re-import of an album, or the
    same track asked by a library-wide run, pays no request and no wait.

    The lock RESERVES each caller's issue slot and is released BEFORE the
    request goes out, so a request's whole latency no longer sits inside the
    critical section — parallel callers (a track's /get and the album's next)
    overlap their network time while the ISSUE rate stays one per 0.4 s. The
    old shape held the lock across `httpx.get`, which made the effective rate
    one per (0.4 s + latency) and the requests strictly serial — the largest
    single cost of Auto tagging."""
    global _lrclib_last
    key = _lrclib_cache_key(endpoint, params)
    cached = _lrclib_cache_read(key)
    if cached is not None:
        return cached
    for attempt in range(retries):
        with _lrclib_lock:
            now = time.time()
            slot = max(now, _lrclib_last + 0.4)
            _lrclib_last = slot
        delay = slot - now
        if delay > 0:
            time.sleep(delay)
        r = httpx.get(f"{LRCLIB_BASE}/{endpoint}", params=params,
                      headers=_LRCLIB_HEADERS, timeout=timeout)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(2.0 * (attempt + 1))
            continue
        # 200 (a hit), 400/404 (no such track — the honest negative the ladder
        # already pays for once per run) are definitive and worth keeping; a
        # transient failure is NOT, so it is asked again next time.
        if r.status_code in (200, 400, 404):
            try:
                body = r.json()
            except Exception:
                body = None
            _lrclib_cache_write(key, r.status_code, body)
            return _LRCLIBCachedResponse(r.status_code, body)
        return r
    return r


# --------------------------------------------------------------------------- #
# covers.musichoarders.xyz (COV) — album cover meta-search
# --------------------------------------------------------------------------- #
# COV aggregates cover art from streaming services and databases. Its search
# endpoint is the same one the website's frontend calls: a POST that streams
# newline-delimited JSON events (source/cover/count/done/error).
COV_BASE = "https://covers.musichoarders.xyz"
# The site's API gate rejects non-browser User-Agents (401), so COV
# requests use a plain browser UA while MB/LRCLIB keep the app UA.
COV_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
# The API allows at most 9 active sources per search; prefer high-quality
# art sources first and fill up with whatever else is enabled.
COV_SOURCE_PRIORITY = list(_cover_choice.DEFAULT_SOURCE_ORDER)
COV_MAX_SOURCES = 9
COV_FALLBACK_SOURCES = list(_cover_choice.DEFAULT_SOURCE_ORDER)
# Sources the meta-search never asks by DEFAULT, even when COV reports them
# enabled: the Cover Art Archive's MusicBrainz source is a user-upload
# database, not a store, so it is off the shipped list. A caller or a saved
# `cover_sources` that names it explicitly is still honoured — it is validated
# against the catalogue like every other id.
COV_DEFAULT_DISABLED = ("musicbrainz",)

_cov_info_cache = {"at": 0.0, "info": {}}
_cov_sources_cache = {"at": 0.0, "ids": []}
# COV's own default when a request omits `country`.
COV_DEFAULT_COUNTRY = "us"


def cov_catalog(timeout=15.0):
    """Everything the UI needs to configure a cover search: the selectable
    sources, the regions, and COV's own active-source cap.

    Cached for an hour — it is static metadata, and the UI asks for it every
    time the finder opens. A failure falls back to the built-in source list so
    the finder still works offline (with the region list reduced to the one
    default, since the real list comes from the server)."""
    import time as _time
    now = _time.time()
    if _cov_info_cache["info"] and now - _cov_info_cache["at"] < 3600:
        return _cov_info_cache["info"]
    try:
        raw = httpx.get(f"{COV_BASE}/api/info",
                        headers={"User-Agent": COV_UA},
                        timeout=timeout).json()
    except Exception:
        raw = {}
    sources = []
    for s in raw.get("sources") or []:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        sources.append({
            "id": s["id"],
            "name": s.get("name") or s["id"],
            "enabled": bool(s.get("enabled", True)),
            "color": s.get("color"),
            "countries": [str(c).lower() for c in (s.get("countries") or [])],
        })
    if not sources:
        sources = [{"id": i, "name": i, "enabled": True, "color": None,
                    "countries": []} for i in COV_FALLBACK_SOURCES]
    countries = [str(c).lower() for c in (raw.get("countries") or [])]
    info = {
        "sources": sources,
        "countries": countries or [COV_DEFAULT_COUNTRY],
        "active_source_limit": int(raw.get("activeSourceLimit") or COV_MAX_SOURCES),
    }
    _cov_info_cache.update(at=now, info=info)
    return info


def _cov_enabled_ids(timeout=15.0):
    """Source ids COV reports as enabled, in priority order — minus the ones
    the app does not ship as defaults (`COV_DEFAULT_DISABLED`)."""
    enabled = [s["id"] for s in cov_catalog(timeout)["sources"]
               if s.get("enabled", True) and s["id"] not in COV_DEFAULT_DISABLED]
    ordered = [s for s in COV_SOURCE_PRIORITY if s in enabled]
    ordered += [s for s in enabled if s not in ordered]
    return ordered or list(COV_FALLBACK_SOURCES)


def _cov_sources(timeout=15.0):
    """Default source ids for a search, capped at COV's active limit."""
    ids = _cov_enabled_ids(timeout)[:cov_catalog(timeout)["active_source_limit"]]
    return ids or list(COV_FALLBACK_SOURCES)


def resolve_cov_search(sources=None, country=None, cfg=None):
    """(source_ids, country) for a search, honouring the caller, then the
    saved defaults, then COV's own defaults.

    `sources`/`country` come from the finder UI (a per-search override);
    `cfg` holds the SAVED defaults (`cover_sources`, `cover_country`) used
    when the caller passes nothing. Every id is validated against the catalog
    so a stale saved setting cannot silently search nothing, and the list is
    trimmed to COV's cap — it rejects a longer one outright."""
    cat = cov_catalog()
    known = {s["id"] for s in cat["sources"]}
    limit = cat["active_source_limit"]

    chosen = [str(s).strip() for s in (sources or []) if str(s).strip()]
    if not chosen:
        try:
            from mlo.config import load_config
            chosen = [str(s).strip() for s in
                      ((cfg or load_config()).get("cover_sources") or [])
                      if str(s).strip()]
        except Exception:
            chosen = []
    chosen = [s for s in chosen if s in known]
    if not chosen:
        chosen = _cov_enabled_ids()

    c = str(country or "").strip().lower()
    if not c:
        try:
            from mlo.config import load_config
            c = str((cfg or load_config()).get("cover_country") or "").strip().lower()
        except Exception:
            c = ""
    if c not in cat["countries"]:
        c = COV_DEFAULT_COUNTRY
    return chosen[:limit], c


def _cover_row(source, small, big, title=None, artist=None, tracks=None,
               url=None, width=None, height=None, front=None, kind=None,
               release_cover=None, rank=None, format=None, nbytes=None,
               cover_likeness=None, cover_blank=None):
    """One search result. Every provider (COV and each fallback) answers this
    exact shape, so the finder never has to know which one answered: the keys
    it already reads (`source`, `small`, `big`, `title`, `artist`, `tracks`,
    `url`) plus what the candidate was MEASURED from rather than what its URL
    suggests.

    `width`/`height` are the REAL pixel size read from the image's own header
    bytes (``None`` means unknown, never a guess) and `format`/`nbytes` are
    what that same probe found — the container the bytes really are and how
    many bytes the URL answered with (0 = an empty answer). `front`/`kind` are
    the provider's own labelling ("front" / "back" / "other"), and
    `release_cover` says whether this is the release GROUP's image (False —
    the album's own art, which the policy prefers) or one RELEASE's own front
    cover (True, an edition's art) or neither stated (``None``).
    `rank` is the provider's own order (0 = its first answer), which
    `mlo.cover_choice` reads as its last tiebreak. `cover_likeness` (0..1) and
    `cover_blank` are filled afterwards by `_attach_looks` — how much detail
    the image itself has, and whether it is a near-solid card the policy
    rejects."""
    return {"source": source, "small": small or None, "big": big or None,
            "title": title or None, "artist": artist or None, "tracks": tracks,
            "url": url or None, "width": width, "height": height,
            "format": format, "bytes": nbytes, "front": front,
            "kind": kind, "release_cover": release_cover, "rank": rank,
            "cover_likeness": cover_likeness, "cover_blank": cover_blank}


def _cov_headers():
    """The exact header set COV's own frontend sends.

    The API gate 401s without it, and it must be these headers ONLY: adding
    `Accept: application/json` makes the endpoint answer 200 with an EMPTY
    stream (verified live), which reads as "no covers exist" — the silent
    nothing this module exists to avoid.
    """
    return {"User-Agent": COV_UA, "Referer": f"{COV_BASE}/", "Origin": COV_BASE,
            "X-Session": uuid.uuid4().hex}


@contextlib.contextmanager
def _cov_stream(body, headers, timeout=60.0):
    """POST a search and yield COV's streamed JSON lines (the HTTP seam)."""
    with httpclient.client().stream(
            "POST", f"{COV_BASE}/api/search", json=body, headers=headers,
            timeout=httpx.Timeout(timeout, read=timeout)) as r:
        r.raise_for_status()
        yield r.iter_lines()


_CAA_GROUP_URL_RE = re.compile(r"coverartarchive\.org/release-group/", re.I)
_CAA_RELEASE_URL_RE = re.compile(r"coverartarchive\.org/release/([0-9a-f-]{36})/", re.I)
_CAA_BACK_URL_RE = re.compile(r"/back(?:-\d+)?(?:\.\w+)?(?:$|[?#])", re.I)
_CAA_FRONT_URL_RE = re.compile(r"/front(?:-\d+)?(?:\.\w+)?(?:$|[?#])", re.I)


def cover_url_labels(url):
    """(kind, release_cover) a URL itself states, or (None, None).

    Only the Cover Art Archive's URLs say this much for free, and they are the
    only ones the app can read without the provider's own metadata:
    `/release-group/<rg>/…` is the GROUP's image — the album's own art, and the
    reference the cover policy prefers, `/release/<mbid>/front…` is that one
    release's own front cover (a specific edition's art, which the policy ranks
    below a name-searched row), and a `/back` names the back. A store's CDN
    path states nothing, which the cover policy treats as unknown rather than
    guessing (`mlo.cover_choice`: a name-searched row sits between the two).
    """
    text = str(url or "")
    if "coverartarchive.org" not in text.lower():
        return None, None
    if _CAA_BACK_URL_RE.search(text):
        return "back", False
    if _CAA_FRONT_URL_RE.search(text):
        return "front", False if _CAA_GROUP_URL_RE.search(text) else True
    if _CAA_GROUP_URL_RE.search(text):
        return None, False               # a group image with no type stated
    if _CAA_RELEASE_URL_RE.search(text):
        return None, True                # one release's own image
    return None, None


def _cov_results(artist, album, limit, timeout, src_ids, ctry):
    """COV's own covers for one query, in the site's relevance order.

    COV states no dimensions and no type: `width`/`height`/`format` are filled
    afterwards by the probe (`_attach_dimensions`), `rank` is the position in
    this very order (COV's own relevance is the last tiebreak the cover policy
    reads), and the front/stand-in labels are read from the URL's own shape
    where CAA URLs state it.

    Each row keeps its event's own `releaseInfo` — the title, artist and track
    count of the release that source matched — because a name search answers
    with OTHER releases too (karaoke, tribute, 8-bit, another album by the same
    artist), and that block is what says which release a cover belongs to.
    `mlo.cover_choice`'s album-identity rule is what reads it.
    """
    body = {"country": ctry, "sources": src_ids}
    if artist:
        body["artist"] = artist
    if album:
        body["album"] = album
    rows = []
    with _cov_stream(body, _cov_headers(), timeout) as lines:
        for line in lines:
            line = (line or "").strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") != "cover":
                continue
            rel = ev.get("releaseInfo") or {}
            big = ev.get("bigCoverUrl")
            kind, release_cover = cover_url_labels(big)
            rows.append(_cover_row(
                ev.get("source"), ev.get("smallCoverUrl"), big,
                rel.get("title"), rel.get("artist"), rel.get("tracks"),
                rel.get("url"), ev.get("width"), ev.get("height"),
                front=True if kind == "front" else (False if kind == "back" else None),
                kind=kind, release_cover=release_cover, rank=len(rows)))
            if len(rows) >= limit:
                break
    return rows


# --------------------------------------------------------------------------- #
# Real image dimensions
# --------------------------------------------------------------------------- #
# COV's streamed lines carry no dimensions at all, and a CDN URL's own
# "500x0w"/"-250" is a REQUEST hint, not the size the URL answers with — so the
# size is read from the image's own header bytes. A ranged GET of the first
# 64 KB is enough for every JPEG SOF marker (they sit near the front), a PNG
# IHDR and all three WebP headers; anything larger is a download, and a search
# must not become one.
COVER_PROBE_BYTES = 65536
# Only the first results are probed — 40 covers must not become 40 image
# fetches. The rest keep `width`/`height` None, which the finder reads as
# "unknown" and shows as such.
COVER_PROBE_LIMIT = 24
COVER_PROBE_WORKERS = 8
# The cover-likeness probe: how much detail the image itself has. A thumbnail
# is small enough to be fetched WHOLE through the same ranged-GET seam (a
# truncated full-size JPEG would not decode), then converted to grayscale and
# downscaled before its edge energy is read. A near-solid image (a colour card,
# a placeholder) has almost no neighbour difference and is not cover art.
COVER_LOOK_BYTES = 262144
COVER_LOOK_SIZE = 64
COVER_LOOK_MIN_DETAIL = 2.0        # mean |Δ| on 0..255 below this = near-solid
COVER_LOOK_DETAIL_REF = 16.0       # the detail a clean, detailed cover shows


def _image_size(data):
    """(width, height) read from an image's own header bytes, or None.

    JPEG (SOI + SOFn), PNG (IHDR) and WebP (VP8X / VP8 / VP8L) only — those
    are what every cover provider here serves; anything else reads unknown
    rather than being guessed at.
    """
    if len(data) < 26:
        return None
    if data[:2] == b"\xff\xd8":                              # JPEG
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker == 0xDA:                               # scan: no SOF after
                break
            seg = int.from_bytes(data[i + 2:i + 4], "big")
            if seg < 2:
                break
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (int.from_bytes(data[i + 7:i + 9], "big"),
                        int.from_bytes(data[i + 5:i + 7], "big"))
            i += 2 + seg
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return (int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"))
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X":                                  # extended
            return (int.from_bytes(data[24:27], "little") + 1,
                    int.from_bytes(data[27:30], "little") + 1)
        if kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":   # lossy
            return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                    int.from_bytes(data[28:30], "little") & 0x3FFF)
        if kind == b"VP8L" and data[20] == 0x2F:             # lossless
            bits = int.from_bytes(data[21:25], "little")
            return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    return None


def _image_format(data):
    """"jpeg"/"png"/"webp" for bytes whose container is one of those, else "".

    The same magic numbers `_image_size` reads, and the answer the cover policy
    judges the format tier on — a PNG photo is re-encoded when it is written,
    a JPEG is already the shape the library stores. Bytes that are not one of
    the three (an HTML error page, a GIF, something exotic) say "" instead of
    being called a cover.
    """
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def _probe_get(url, nbytes=COVER_PROBE_BYTES, timeout=10.0):
    """The first *nbytes* of an image URL (a Range request).

    b"" when the URL ANSWERED with nothing (an empty body, a 404 body-less
    reply) and None when the request itself failed (a dead host, a refused
    connection, a timeout) — the difference between "this URL has no image" and
    "this URL could not be asked", which the cover policy needs: an empty
    answer is a candidate it rejects out loud, an unasked one is merely
    unknown. Never raises: a probe that cannot answer is a valid answer.

    Range is a request, not a promise: a server may answer 200 with the whole
    file, so the body is read in chunks and dropped once *nbytes* are in hand.
    """
    try:
        # The shared client (server/httpclient): the redirect and the read
        # timeout are this request's own, so a probe behaves exactly as the
        # per-call client did.
        with httpclient.client().stream(
                "GET", url,
                headers={"Range": f"bytes=0-{nbytes - 1}",
                         "User-Agent": COV_UA},
                timeout=httpx.Timeout(timeout, read=timeout),
                follow_redirects=True) as r:
            r.raise_for_status()
            out = bytearray()
            for chunk in r.iter_bytes(nbytes):
                out += chunk
                if len(out) >= nbytes:
                    break
            return bytes(out[:nbytes])
    except Exception:
        return None


def _probe_dimensions(url, timeout=10.0):
    """One probe of one URL: header bytes only, public hosts only, no raise.

    Answers ``{"width", "height", "format", "bytes"}`` — what the image's own
    bytes say it is (a dimension pair, a container) plus how many bytes the URL
    answered with, which is how an empty answer (0 bytes) is told apart from
    bytes that are not an image at all. A URL that cannot be probed at all
    answers None (unknown), never a guess.

    Nothing here may raise — `_genre_cached` requires its producer to be total,
    and an unknown size is a valid answer while a dead search is not.
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if (parsed.scheme not in ("http", "https")
                or not _public_host(parsed.hostname)):
            return None                 # the same trust boundary as the download
        data = _probe_get(url, timeout=timeout)
        if data is None:
            return None                 # the URL could not be asked at all
        if not data:
            # An Apple URL whose own copy is missing (HTTP 200, empty body) is
            # FETCHED, and therefore written, as the same artwork's largest
            # transform (`server.artcache`'s own repair), so that is the copy
            # this measures. Reporting the dead URL as an image with nothing
            # behind it would reject a row the app can serve — and the size the
            # policy ranks on would describe a file nobody can download.
            bigger = _artwork_big(url)
            if bigger and bigger != url:
                data = _probe_get(bigger, timeout=timeout) or b""
        size = _image_size(data)
        out = {"format": _image_format(data), "bytes": len(data)}
        if size:
            out["width"], out["height"] = size[0], size[1]
        return out
    except Exception:
        return None


def image_dimensions(url, timeout=10.0):
    """The image at *url* as the probe found it, or None when unknown.

    ``{"width", "height", "format", "bytes"}`` — the real pixel size, the real
    container and how many bytes the URL answered with (0 = an empty answer).
    Memoized 30 days (memory + <music>/.mlo/data, the app's shared value
    cache) — a probe that found nothing is cached too, so a rerun never re-asks
    a host that already said no.
    """
    url = str(url or "").strip()
    if not url:
        return None
    # The memo's OWN kind carries the producer's shape: an Apple URL that
    # answers empty is now measured at the same artwork's largest copy (the one
    # the app actually fetches), and an answer stored before that change would
    # report a row's image as missing while it can be had.
    got = _genre_cached("cover_dim2", url,
                        lambda: _probe_dimensions(url, timeout))
    return got if isinstance(got, dict) else None


def _probe_targets(rows, limit):
    """The rows a probe must measure: the first *limit*, PLUS every row that
    carries the album's own art.

    The bound exists so a search does not become an image downloader — but the
    IDENTITY rows (the Cover Art Archive's release-group and release fronts)
    are appended AFTER the meta-search's rows, so a plain ``rows[:limit]``
    slice left them unmeasured — and an unmeasured row is REJECTED while a
    cover minimum is set, which made the album's own art unpickable on any
    album whose name search returned 24+ rows (measured live 2026-10-10:
    Plastic Beach, 52 rows, the release group's front rejected unmeasured and a
    store's re-issue sleeve picked instead). They are a handful of rows, so
    they are always measured.
    """
    out = []
    for i, row in enumerate(rows or ()):
        if not isinstance(row, dict):
            continue
        if i < limit or row.get("release_cover") is not None or row.get("front"):
            out.append(row)
    return out


def _attach_dimensions(rows, limit=COVER_PROBE_LIMIT, timeout=10.0):
    """Fill what each row's own image says: size, container, byte count.

    The rows `_probe_targets` names — the first *limit*, plus the identity rows
    — probed in a small pool — these are 20+ different CDNs, and a search that
    waits on them one at a time is a search that hangs on one slow host. A row
    COV already sized is probed only when its CONTAINER is still unknown (the
    format tier of the cover policy reads it, and a row whose size came from
    COV's own line has no format yet); a row that answers nothing at all keeps
    `bytes: 0`, which is the one thing the policy must be able to tell apart
    from "never asked".
    """
    todo = [r for r in _probe_targets(rows, limit)
            if r.get("width") is None or r.get("format") is None]
    if not todo:
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=COVER_PROBE_WORKERS) as pool:
        sizes = list(pool.map(
            lambda r: image_dimensions(r.get("big") or r.get("small"), timeout),
            todo))
    for row, size in zip(todo, sizes):
        if not size:
            continue
        if size.get("width"):
            # The probe's OWN reading wins over anything the provider stated:
            # that number describes the file the provider holds, while this one
            # describes the bytes this URL answers with. ``None`` (unknown) stays
            # unknown either way.
            row["width"], row["height"] = size["width"], size["height"]
        if "format" in size:
            # "" is an answer too: the bytes are not a JPEG/PNG/WebP image.
            row["format"] = size["format"]
        if size.get("bytes") is not None:
            row["bytes"] = size["bytes"]


def _image_look(data):
    """``{"likeness", "blank", "phash"}`` for image *data*, or None when it
    cannot be read.

    The metric `mlo.cover_choice`'s appearance tier is: decode the bytes with
    Pillow, convert to grayscale, downscale to `COVER_LOOK_SIZE`, and read the
    mean absolute difference between neighbouring pixels (edge energy) in both
    directions. Below `COVER_LOOK_MIN_DETAIL` there is no structure at all — a
    solid colour card, not artwork — and the image is flagged blank (the
    policy's hard rejection). Otherwise the level is that detail against
    `COVER_LOOK_DETAIL_REF`, capped at 1.0.

    ``phash`` is a 64-bit dHash of the same image (an 8×8 grid of
    left-to-right brightness comparisons, the classic difference hash): two
    encodings of the SAME artwork agree on nearly every bit and two different
    pictures do not, which is what lets `mlo.cover_choice`'s reference tier
    compare a candidate against the album's own MusicBrainz front cover — the
    owner's report was a pick that did not match the reference the finder
    showed beside it. 0 for a flat image (every comparison equal), which the
    blank flag already rejects.

    Bytes this cannot decode (a truncated fetch, an HTML error page, an exotic
    format) are UNKNOWN, not blank: nothing here may claim an image is a colour
    card just because it could not be opened.
    """
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            gray = img.convert("L").resize((COVER_LOOK_SIZE, COVER_LOOK_SIZE))
            # 9×8, so each of the 8 rows yields 8 neighbouring-pixel compares.
            grad = list(img.convert("L").resize((9, 8)).getdata())
        w, h = gray.size
        if w < 2 or h < 2:
            return None
        px = list(gray.getdata())
        dx = sum(abs(px[r * w + i] - px[r * w + i + 1])
                 for r in range(h) for i in range(w - 1)) / float(h * (w - 1))
        dy = sum(abs(px[i] - px[i + w])
                 for i in range((h - 1) * w)) / float((h - 1) * w)
        detail = (dx + dy) / 2.0
        bits = 0
        for row in range(8):
            base = row * 9
            for i in range(8):
                bits = (bits << 1) | (1 if grad[base + i] > grad[base + i + 1] else 0)
        if detail < COVER_LOOK_MIN_DETAIL:
            return {"likeness": 0.0, "blank": True, "phash": bits}
        return {"likeness": round(min(1.0, detail / COVER_LOOK_DETAIL_REF), 4),
                "blank": False, "phash": bits}
    except Exception:
        return None


def _probe_look(url, timeout=10.0):
    """One look probe of one URL — public hosts, 256 KB, never raises."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if (parsed.scheme not in ("http", "https")
                or not _public_host(parsed.hostname)):
            return None
        data = _probe_get(url, COVER_LOOK_BYTES, timeout)
        if not data:
            return None
        return _image_look(data)
    except Exception:
        return None


def cover_look(url, timeout=10.0):
    """The image at *url* as the look probe found it, or None when unknown.

    ``{"likeness", "blank"}`` — how much detail the image has and whether it is
    a near-solid card. Memoized 30 days exactly like `image_dimensions` (memory
    + the app's shared value cache), so the finder dialog and the unattended
    import measure once and rank identically.
    """
    url = str(url or "").strip()
    if not url:
        return None
    got = _genre_cached("cover_look", url, lambda: _probe_look(url, timeout))
    return got if isinstance(got, dict) else None


def _row_look(row, timeout=10.0):
    """The row's own look: its `small` thumbnail first (small enough to be
    fetched whole), then its `big` image as the fallback."""
    for url in (row.get("small"), row.get("big")):
        if not url:
            continue
        got = cover_look(url, timeout)
        if got is not None:
            return got
    return None


def _attach_looks(rows, limit=COVER_PROBE_LIMIT, timeout=10.0):
    """Fill each row's own cover-likeness and perceptual hash.

    The rows `_probe_targets` names (the first *limit*, plus the identity rows
    — see that function for why the reference rows may not be skipped), in the
    same small pool as `_attach_dimensions`: the same bound and the same
    reason: a search must not become an image downloader. A row nobody measured
    keeps `cover_likeness` None, and `mlo.cover_choice` scores it at the set's
    median looks rather than blaming it. A near-solid image gets `cover_blank`,
    which the policy rejects. `phash` is the row's 64-bit dHash, the term the
    policy's reference tier compares with the album's own art.
    """
    todo = [r for r in _probe_targets(rows, limit)
            if r.get("cover_likeness") is None and not r.get("cover_blank")]
    if not todo:
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=COVER_PROBE_WORKERS) as pool:
        looks = list(pool.map(lambda r: _row_look(r, timeout), todo))
    for row, got in zip(todo, looks):
        if not got:
            continue
        row["cover_likeness"] = got.get("likeness")
        if got.get("blank"):
            row["cover_blank"] = True
        if isinstance(got.get("phash"), int) and not isinstance(got.get("phash"), bool):
            row["phash"] = got["phash"]


# --------------------------------------------------------------------------- #
# The identity providers — asked by ID, not by name
# --------------------------------------------------------------------------- #
# The meta-search (COV) is a NAME search: it is fast, it carries the big store
# artwork, and it can also answer with another artist's album. The Cover Art
# Archive is an IDENTITY read: asked about the release GROUP it answers with
# the album's own front cover — the reference `mlo.cover_choice` prefers and
# the one the finder shows the candidates beside — and asked about a specific
# release it answers with that edition's own front cover. Both are asked when
# both are known, and the group's image is what wins: one pressing's sleeve is
# not the album's art.
#
# The fallbacks below are reached only when the name search has nothing; each
# is a single lookup, and each states in the search's `sources` report what it
# did. The Cover Art Archive is NOT among them any more: its identity reads run
# above whenever an id is known, and it is not a store whose artwork should
# quietly stand in for one that refused.
CAA_BASE = "https://coverartarchive.org"
DEEZER_API = "https://api.deezer.com"
COVER_FALLBACKS = ("deezer", "itunes")


# Apple serves one artwork asset under as many addresses as its stores name
# it: the search's `artworkUrl100`, the cover finder's storefront URLs
# (`a1.mzstatic.com/r40/…/<id>.png` plus the same asset's `…/<id>.png/500x0w.webp`
# thumbnail), and the `image/thumb` address that serves any size of it. The
# storefront's unmodified copy answers HTTP 200 with an EMPTY body when it is
# not there (verified live: it is for In Rainbows' assets, while the SAME
# asset's `image/thumb` transform answers 1.8 MB), so the shape is understood
# here in one place, and both shapes are answered in the one form this app
# fetches.
_APPLE_ART_RE = re.compile(
    r"^https?://(?:[\w.-]+\.)*mzstatic\.com/(?:image/thumb/|r40/|us/r30/)(.+)$", re.I)
# A transform's size/crop segment — `3000x3000bb.jpg`, `500x0w.webp` — never the
# asset's own file name (`634904032463.png`, `dj.dmofttoi.jpg`), which carries
# no `<w>x<h>` at all.
_APPLE_SIZE_RE = re.compile(r"/\d+x\d+\w*\.\w{2,5}$", re.I)
_APPLE_BIG_FMT = "https://is1-ssl.mzstatic.com/image/thumb/%s/3000x3000bb.jpg"


def _artwork_big(url):
    """The largest copy of the SAME Apple artwork at *url*, or None.

    `…/100x100bb.jpg` → `…/3000x3000bb.jpg` (Apple's largest artwork): one
    asset, every size in that segment. None for a URL that is not an Apple
    artwork address — there is nothing to repair, and a caller must not treat
    one store's URL as another's.
    """
    m = _APPLE_ART_RE.match(str(url or "").strip())
    if not m:
        return None
    path = _APPLE_SIZE_RE.sub("", "/" + m.group(1)).lstrip("/")
    return _APPLE_BIG_FMT % path if path else None


def _name_rank(name, want):
    """0 when *name* is exactly *want* (folded), else 1 — sorting only.

    Whether a row is THIS album is not decided here: a search is allowed to
    answer with a tribute, a karaoke or an 8-bit release carrying the same
    names, and `mlo.cover_choice`'s album-identity rule rejects such a row
    (with its own stated artist/title/tracks, which is why every row carries
    them). Sorting only means exactly that — a name that does not match does
    not hide the row, it puts it later.
    """
    return 0 if (want and _norm_compare(name) == want) else 1


def _caa_rows(mbid, scope, limit, artist, album, timeout, release_cover):
    """CAA images for one id, front cover first, labelled with what they are.

    `scope` is "release" or "release-group" — the endpoint, and therefore the
    answer's meaning: a group's images are the album's own art
    (`release_cover` False, the reference the policy prefers), while a
    specific release's are that edition's own front/back/other images
    (`release_cover` True). CAA publishes no dimensions, which is exactly why
    the probe exists.
    """
    ident = str(mbid or "").strip()
    if not ident:
        return []
    data = _advisory_json(f"{CAA_BASE}/{scope}/{ident}",
                          headers={"User-Agent": USER_AGENT},
                          timeout=timeout, host="coverartarchive.org")
    images = [i for i in ((data or {}).get("images") or []) if i.get("image")]
    images.sort(key=lambda i: not i.get("front"))
    page = f"{CAA_BASE}/{scope}/{ident}"
    rows = []
    for i, img in enumerate(images[:limit]):
        th = img.get("thumbnails") or {}
        types = [str(t).strip().lower() for t in (img.get("types") or [])]
        front = bool(img.get("front"))
        kind = ("front" if front else
                "back" if "back" in types else
                types[0] if types else "")
        rows.append(_cover_row(
            "coverartarchive",
            th.get("large") or th.get("small") or img["image"],
            img["image"], album, artist, None, page,
            front=front, kind=kind, release_cover=release_cover, rank=i))
    return rows


def _caa_release_covers(release_mbid, limit, artist="", album="", timeout=30.0):
    """The RELEASE's own images (its front cover first) — the identity read."""
    return _caa_rows(release_mbid, "release", limit, artist, album, timeout, True)


def _caa_covers(rg_mbid, limit, artist="", album="", timeout=30.0):
    """Cover Art Archive images for a RELEASE GROUP, front cover first.

    Asked about the release-group MBID the caller has (the import wizard holds
    one): CAA is browsed BY IDENTITY — its search is per release, and matching
    it by name is the name-guessing this app does not do. These are stand-ins
    for the group, not the release's own cover, and the policy says so.
    """
    return _caa_rows(rg_mbid, "release-group", limit, artist, album, timeout, False)


def _deezer_covers(artist, album, limit, timeout=30.0):
    """Deezer's album cover — `cover_xl` is the biggest Deezer serves
    (1000×1000), so nothing here is expected to be larger than that.

    Rows are ORDERED, never dropped: a tribute/karaoke album that carries the
    same title is a bad first hit, not a reason to hide the real covers. The
    matched album's own artwork is a front cover of THAT album (not a group
    stand-in), which is what `release_cover` says — and every row keeps the
    matched album's own title/artist/`nb_tracks`, so `mlo.cover_choice` can
    reject a row whose release is not the album being covered.
    """
    term = " ".join(x for x in (f'artist:"{artist}"' if artist else "",
                                f'album:"{album}"' if album else "") if x)
    if not term:
        return []
    data = _advisory_json(f"{DEEZER_API}/search/album",
                          {"q": term, "limit": max(1, min(limit, 25))},
                          timeout=timeout, host="api.deezer.com")
    want_artist, want_album = _norm_compare(artist), _norm_compare(album)
    rows = [a for a in ((data or {}).get("data") or [])
            if a.get("cover_xl") or a.get("cover_big")]
    rows.sort(key=lambda a: (_name_rank((a.get("artist") or {}).get("name"),
                                        want_artist),
                             _name_rank(a.get("title"), want_album)))
    return [_cover_row("deezer", a.get("cover_big") or a.get("cover_medium"),
                       a.get("cover_xl") or a.get("cover_big"),
                       a.get("title"), (a.get("artist") or {}).get("name"),
                       a.get("nb_tracks"), a.get("link"),
                       front=True, kind="front", release_cover=True, rank=i)
            for i, a in enumerate(rows[:limit])]


def _itunes_covers(artist, album, limit, cfg=None, timeout=30.0):
    """Apple's album artwork at its 3000×3000 size (see `_artwork_big`).

    Reached through the app's cached, throttled iTunes client — this is the
    same endpoint the advisory routes already use, and Apple rate-limits.
    """
    term = " ".join(x for x in (artist, album) if x)
    if not term:
        return []
    data = _apple_json("/search", {"term": term, "entity": "album",
                                   "limit": max(1, min(limit, 25)),
                                   "country": _apple_country(cfg)},
                       timeout=timeout)
    want_artist, want_album = _norm_compare(artist), _norm_compare(album)
    rows = [a for a in ((data or {}).get("results") or [])
            if a.get("artworkUrl100")]
    rows.sort(key=lambda a: (_name_rank(a.get("artistName"), want_artist),
                             _name_rank(a.get("collectionName"), want_album)))
    return [_cover_row("itunes", a["artworkUrl100"],
                       _artwork_big(a["artworkUrl100"]) or a["artworkUrl100"],
                       a.get("collectionName"), a.get("artistName"),
                       a.get("trackCount"), a.get("collectionViewUrl"),
                       front=True, kind="front", release_cover=True, rank=i)
            for i, a in enumerate(rows[:limit])]


def _source_row(pid, status, *, count=None, detail=""):
    """One line of the search's own report: who was asked, and what happened.

    `status` is "used" (it answered with candidates), "empty" (it answered with
    nothing), "error" (it refused or failed) or "skipped" (it was not asked at
    all — and `detail` says why: no id to ask about, no key configured, the
    source switched off). A source that contributed nothing is never left
    silent: the covers policy turns these into the pick's own notes.
    """
    row = {"id": pid, "status": status}
    if count is not None:
        row["count"] = int(count)
    if detail:
        row["detail"] = str(detail)[:300]
    return row


def _cover_fallback(artist, album, limit, cfg, timeout):
    """(rows, provider, report) from the FIRST fallback that answers.

    Only ever reached when the meta-search and the release's own cover came
    back empty: one lookup per provider in COVER_FALLBACKS order, stopping at
    the first non-empty list. A provider that errors, or that has no identity
    to be asked about at all, says so in the report instead of being skipped in
    silence — and none of them is fatal.
    """
    report = []
    for pid in COVER_FALLBACKS:
        try:
            if pid == "deezer":
                rows = _deezer_covers(artist, album, limit, timeout)
            else:
                rows = _itunes_covers(artist, album, limit, cfg, timeout)
        except Exception as e:
            report.append(_source_row(pid, "error", detail=f"{type(e).__name__}: {e}"))
            continue
        report.append(_source_row(pid, "used" if rows else "empty", count=len(rows)))
        if rows:
            return rows, pid, report
    return [], None, report


def cover_search(artist, album, limit=_cover_choice.SEARCH_LIMIT, timeout=60.0, sources=None,
                 country=None, cfg=None, release_group_mbid="", release_mbid=""):
    """Album covers for artist/album → ``{"results", "provider", "sources"}``.

    Every row is ``{source, small, big, title, artist, tracks, url, width,
    height, format, bytes, front, kind, release_cover, rank, cover_likeness,
    cover_blank}``: the keys the finder already reads, plus what the candidate
    was MEASURED from — the image's real pixel size, container and byte count,
    read from the file itself for the first ``COVER_PROBE_LIMIT`` rows
    (``None`` means unknown — a probe can never fail the search), whether the
    provider labels it the front cover of the release it belongs to, and its
    own rank in that provider's order.

    ``sources`` is the report `mlo.cover_choice.source_notes` turns into the
    pick's notes: one row per source that was asked — used / empty / error /
    skipped, with the reason — so a query that found nothing says what was
    tried and which source was never asked (no id, no key, switched off).

    ``provider`` names who answered first: ``"cov"`` for the meta-search, the
    fallback id that filled in (``deezer``/``itunes``), and ``None`` when
    nobody had anything at all — an empty answer is STATED, never left as a
    silent zero-result.

    Every row is also MEASURED for its own cover-likeness (`_attach_looks`,
    memoized like the size probe): a near-solid image is flagged blank and the
    policy rejects it, and any other measured image carries a 0..1
    `cover_likeness` `mlo.cover_choice`'s appearance tier weighs with the size.

    ``sources``/``country`` override the SAVED defaults (`cover_sources`,
    `cover_country`) for this one search only — nothing here writes config.
    ``release_group_mbid``/``release_mbid``, when the caller has them, are the
    identities the Cover Art Archive is asked about: the album's own front
    cover by the GROUP id (the reference the policy prefers) and one specific
    release's own by the release id. A caller holding only the RELEASE id gets
    the group resolved here (`release_lookup`), because that reference is what
    `mlo.cover_choice`'s first rule is built around: without it the search could
    only ever offer a name-searched row or one edition's sleeve, and the
    unattended import and the finder's dialog would be ranking different
    candidate sets for the same album. The lookup is cached, and one that fails
    leaves the identity as it was — a cover search never fails over it. The
    name-based fallbacks (Deezer, iTunes) run only when the meta-search and the
    identity read both came back empty (or refused) — a dead meta-search is a
    fallback case, not an error the user has to understand.

    ``limit`` is how many candidates to ask for, and its default is the ONE
    number every caller passes (`mlo.cover_choice.SEARCH_LIMIT`): the answer is
    truncated at it, so a caller asking for a different amount is ranking a
    different set of rows.
    """
    if not artist and not album:
        raise ValueError("artist or album is required")
    if not str(release_group_mbid or "").strip() and str(release_mbid or "").strip():
        try:
            release_group_mbid = str(
                (release_lookup(str(release_mbid).strip()) or {}
                 ).get("release_group_id") or "").strip()
        except Exception:
            release_group_mbid = ""       # an outage is not a search failure
    src_ids, ctry = resolve_cov_search(sources, country, cfg)
    report = []
    # A source the catalogue reports as switched off is never asked, and that
    # is stated rather than looked like "it had nothing" (several of these
    # sources need a key the catalogue's operator holds, not the user).
    try:
        enabled = {str(s.get("id") or ""): bool(s.get("enabled", True))
                   for s in (cov_catalog(timeout).get("sources") or [])}
        for sid in src_ids:
            if enabled.get(sid) is False:
                report.append(_source_row(
                    sid, "skipped",
                    detail="switched off in the source catalogue — not asked"))
    except Exception:
        pass
    results = []
    try:
        cov = _cov_results(artist, album, limit, timeout, src_ids, ctry)
    except Exception as e:
        cov = []
        report.append(_source_row("covers.musichoarders.xyz", "error",
                                  detail=f"{type(e).__name__}: {e}"))
    else:
        counts = {}
        for r in cov:
            counts[str(r.get("source") or "?")] = counts.get(str(r.get("source") or "?"), 0) + 1
        report.append(_source_row(
            "covers.musichoarders.xyz", "used" if cov else "empty", count=len(cov),
            detail=", ".join(f"{k} {v}" for k, v in sorted(counts.items()))))
    results.extend(cov)
    provider = "cov" if cov else None

    # The album's own art first: the Cover Art Archive asked about the release
    # GROUP. It is the reference the finder shows beside the candidates and the
    # one the policy prefers, so it is asked whenever the caller knows the
    # group id — leaving it to the fallback chain (its old place) meant a
    # release with its own cover never produced the reference at all, and the
    # pick could only ever be a single edition's sleeve.
    if str(release_group_mbid or "").strip():
        try:
            ref = _caa_covers(release_group_mbid, limit, artist, album, timeout)
        except Exception as e:
            ref = []
            report.append(_source_row("coverartarchive", "error",
                                      detail=f"{type(e).__name__}: {e}"))
        else:
            report.append(_source_row(
                "coverartarchive", "used" if ref else "empty", count=len(ref),
                detail="the release group's own images, by release-group id"))
        results.extend(ref)
        if ref and not provider:
            provider = "coverartarchive"
    else:
        report.append(_source_row(
            "coverartarchive", "skipped",
            detail="no release-group id to ask about — only the release"))

    if str(release_mbid or "").strip():
        try:
            own = _caa_release_covers(release_mbid, limit, artist, album, timeout)
        except Exception as e:
            own = []
            report.append(_source_row("coverartarchive", "error",
                                      detail=f"{type(e).__name__}: {e}"))
        else:
            report.append(_source_row("coverartarchive",
                                      "used" if own else "empty", count=len(own),
                                      detail="the release's own images, by release id"))
        results.extend(own)
        if own and not provider:
            provider = "coverartarchive"
    else:
        report.append(_source_row(
            "coverartarchive", "skipped",
            detail="no release id to ask about — only the release group"))

    if not results:
        rows, pid, more = _cover_fallback(artist, album, limit, cfg, timeout)
        report.extend(more)
        results = rows
        provider = pid
    else:
        for pid in COVER_FALLBACKS:
            report.append(_source_row(pid, "skipped",
                                      detail="not asked — the sources above answered"))
    _attach_dimensions(results)
    _attach_looks(results)
    return {"results": results, "provider": provider, "sources": report}


# Image downloads are the one place a caller supplies a URL the server then
# fetches. Provider CDNs do redirect (Cover Art Archive → archive.org), so
# redirects are followed — but each hop is re-validated, the destination must
# be a public host, and the body is capped: artwork that is not a few
# megabytes is a mistake or an attack, not a cover.
IMAGE_MAX_BYTES = 20 * 1024 * 1024
IMAGE_MAX_REDIRECTS = 5


def _public_host(host):
    """False for localhost, loopback, private, link-local, reserved, multicast
    and unspecified addresses — the SSRF guard for caller-supplied URLs."""
    import ipaddress
    import socket
    name = str(host or "").strip().strip("[]")
    if not name or name.lower().endswith(".local"):
        return False
    if name.lower() in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return False
    try:
        infos = socket.getaddrinfo(name, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def fetch_image_bytes(url, timeout=60.0):
    """Download an image URL and return (data, content_type).

    Only http(s) to a *public* host is allowed, every redirect hop is
    re-checked (a redirect chain must not reach an internal address after the
    first check), and the body is capped at IMAGE_MAX_BYTES so a huge or slow
    endpoint cannot be used as a memory/time hold.
    """
    from urllib.parse import urljoin, urlparse
    headers = {"User-Agent": COV_UA, "Referer": f"{COV_BASE}/"}
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("invalid image url")
    if not _public_host(parsed.hostname):
        raise ValueError("image url is not a public host")
    # The shared client (server/httpclient) — never entered as a context
    # manager, which would close the process-wide client on the way out.
    client = httpclient.client()
    for _hop in range(IMAGE_MAX_REDIRECTS + 1):
        with client.stream("GET", url, headers=headers,
                           timeout=httpx.Timeout(timeout, read=timeout),
                           follow_redirects=False) as r:
            if r.is_redirect:
                target = urljoin(url, str(r.headers.get("location") or ""))
                hop = urlparse(target)
                if hop.scheme not in ("http", "https") or not hop.netloc:
                    raise ValueError("invalid image redirect")
                if not _public_host(hop.hostname):
                    raise ValueError("image redirect leaves the public internet")
                url = target
                continue
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            chunks, size = [], 0
            for chunk in r.iter_bytes(65536):
                size += len(chunk)
                if size > IMAGE_MAX_BYTES:
                    raise ValueError("image is too large")
                chunks.append(chunk)
            return b"".join(chunks), ctype
    raise ValueError("too many image redirects")


def lyrics_chain(cfg, artist, track, album=None, duration=None):
    """First provider hit for a track across the lyrics chain (the order lives
    in ``mlo.lyrics_providers.SOURCES``: LRCLIB, NetEase, QQ Music, Kuwo,
    Kugou, YouTube captions) — same dict as
    ``mlo.lyrics_providers.fetch_lyrics``, or None. Imported lazily so the
    engine module stays out of this header."""
    from mlo.lyrics_providers import fetch_lyrics
    return fetch_lyrics(cfg, artist, track, album, duration)
