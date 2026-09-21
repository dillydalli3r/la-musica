"""AI genre ranking — the import's one model call.

`server.integrations.genre_chain` asks MusicBrainz, RateYourMusic and the
other configured sources first and hands their answers here; this module
turns them — plus, when ``ai_genre_research`` is on, the model's own
knowledge of the release — into at most ``mb_genre_count - 1`` SPECIFIC
genres, most specific first. The family is NOT asked for and a family answer
is dropped: the app derives it (`mlo.genre_vocab.parent_of`) and puts it
first, see ``mlo.genres``.

Optional end to end: no endpoint configured, a refusal, a timeout, an
unparseable answer or an answer with nothing usable in it all mean ``None``,
and the caller keeps the source list exactly as it was. Nothing here ever
raises — an import must never fail because the model had a bad day.

``server.ai.ai_chat`` is the same OpenAI-compatible client script 17 uses;
the prompt is built here and passed to it verbatim, and only the effort is
re-pointed at ``ai_genre_effort`` (the client reads ``ai_effort``, which is
script 17's own knob — the two are configured separately and one must not
drag the other).

The answer is cached on disk under that module's cache root, one JSON file
per prompt hash: the same (artist, album, title, candidates, effort,
research) always builds the same prompt, so a re-run of the import, of Auto
tagging, or of a second album pass over the same release costs no second
call. The candidate list is part of the prompt, so a source answering later
with something new yields a different key and a fresh answer — the cache can
never serve a ranking for a question that was not asked.
"""
import hashlib
import json
import os
import re

from server import ai as ai_client
from mlo.genres import (DEFAULT_GENRE_COUNT, GENRE_COUNT_MAX, canonical,
                        is_parent)

SYSTEM = (
    "You are a music taxonomist naming the genres a release is filed under. "
    "You answer ONLY with the JSON object asked for."
)

# The shape the answer must have, spelled out in full: the model answers with
# the SPECIFIC genres only, most specific first — the broad family is the
# app's to derive (`mlo.genre_vocab.parent_of`) and put in front of them, so
# asking for it would be asking for a slot the caller has to throw away, and a
# model left to itself answers with a flat bag of near-synonyms plus a family.
# Kept in the prompt rather than re-derived from the reply, because "which
# genre is more specific" is a judgement no word list reproduces.
_SLOTS = (
    "Answer with AT MOST {count} SPECIFIC genre(s), most specific first — no "
    "numbering, no explanation, JSON only:\n"
    "  at most {count} genre(s) that describe this release, e.g. Post-Britpop, "
    "Shoegaze, Melodic Death Metal.\n"
    "Do NOT name a broad family (Rock, Pop, Electronic, Hip Hop, Jazz, "
    "Classical, Folk, Metal…) in any slot: the app derives the family from the "
    "specific genre and appends it itself, so a family here is discarded.\n"
    "Every name must be a MusicBrainz genre, or one of the fetched genres "
    "above; they must be distinct (never the same word twice, not even with "
    "different capitalisation)."
)

_RESEARCH_ON = (
    "Use your own knowledge of this artist and release: think carefully "
    "about what the record actually is and what it is usually filed under, "
    "and go past the fetched list below when it is thin, stale or plainly "
    "wrong. The fetched list is context, not a menu."
)
_RESEARCH_OFF = (
    "Re-rank ONLY the fetched genres below: every genre you answer with must "
    "appear in that list (you may correct its spelling and its order, never "
    "add a genre it does not contain)."
)


def _config():
    """The app's config, and {} when it cannot be read.

    Loaded lazily (this module must import cleanly without the engine's
    state folder) and NEVER cached: the caller runs one import at a time,
    and the model call it gates costs orders of magnitude more than the
    config read.
    """
    try:
        from mlo.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _prompt(artist, album, title, track_path, candidates, count, extra, research):
    """The user message: what the release is, what is known, what to answer.

    Everything the model may use is in here — the prompt IS the cache key, so
    a value left out of it would be a value the cache could not tell apart
    from a different question.
    """
    lines = [f"Release: {artist or 'unknown artist'} — {album or 'unknown album'}"]
    if title:
        lines.append(f"Track: {title}")
    if track_path:
        lines.append(f"File: {track_path}")
    for key, value in sorted((extra or {}).items()):
        if str(value or "").strip():
            lines.append(f"{key}: {value}")
    names = [str(n).strip() for n in (candidates or []) if str(n).strip()]
    if names:
        lines.append("")
        lines.append("Fetched genres (best source first):")
        lines.extend(f"- {n}" for n in names)
    lines.append("")
    lines.append(_RESEARCH_ON if research else _RESEARCH_OFF)
    lines.append("")
    lines.append(_SLOTS.format(count=count))
    return "\n".join(lines)


def _parse(text):
    """The genre names in an answer, however the model spelled the answer.

    Three shapes are all seen in practice and all accepted: the JSON object
    asked for (possibly fenced), a bare JSON array, and a plain list — one
    genre per line, numbered or bulleted, which is what a model that ignored
    the JSON instruction returns. Anything else yields no names, and the
    caller rejects the answer rather than storing junk as a genre.
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        # Take the first list the object holds: "genres", "Genres",
        # {"genres": {...}} nesting — the key name is the model's choice and
        # pinning one spelling would reject a correct answer.
        for value in data.values():
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _parse(json.dumps(value))
                if nested:
                    return nested
        return [v for v in data.values() if isinstance(v, str)]
    if isinstance(data, list):
        return data
    names = []
    for line in raw.splitlines():
        name = re.sub(r"^[\s\-\*\d.)]+", "", line).strip().strip("\"'").strip()
        if name:
            names.append(name)
    return names


def _cache_file(prompt):
    """Where one prompt's answer lives, under the lyric-AI cache root."""
    try:
        folder = ai_client._cache_dir()
        digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()
        return os.path.join(folder, f"genre-{digest[:20]}.json")
    except Exception:
        return None


def _cached(path, want):
    """The cached list at *path*, or None when absent/unusable.

    A cached entry is only re-served while it still satisfies the minimum the
    caller would enforce on a fresh answer, so a shorter-than-usable list can
    never be pinned forever.
    """
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            names = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(names, list):
        return None
    out = [str(n) for n in names if str(n).strip()]
    return out if len(out) >= want else None


def _store(path, names):
    """Persist one answer; a cache that cannot be written is not an error."""
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with ai_client._CACHE_LOCK:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(names, fh, ensure_ascii=False)
    except OSError:
        pass


def _specifics(names, candidates, limit):
    """The usable SPECIFIC genres of one answer, at most *limit* of them.

    A name is kept when it is a MusicBrainz genre (`mlo.genres.canonical`) or
    one of the fetched candidates (a source's own spelling is fine — the
    writers canonicalize, and dropping what a source said would be worse),
    resolved to MusicBrainz's spelling when it is one, de-duplicated
    case-insensitively, and in the model's own order (which IS the ranking).

    A name that IS a family is dropped rather than returned: the app derives
    and appends the family itself (mlo.genres.normalize_genres), so a family
    here would either duplicate the derived slot or take a specific genre's
    place. Nothing else is filtered — junk the model invented that is neither
    a genre nor a fetched candidate is what the vocabulary grade flags.
    """
    allowed = {str(c).strip().casefold() for c in (candidates or []) if str(c).strip()}
    out, seen = [], set()
    for raw in names:
        text = " ".join(str(raw or "").split())
        if not text:
            continue
        name = canonical(text) or text
        key = name.casefold()
        if key in seen or is_parent(name):
            continue
        if canonical(text) is None and key not in allowed:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= limit:
            break
    return out


def infer_genres(*, artist, album, title="", track_path="", candidates=None,
                 count=DEFAULT_GENRE_COUNT, extra=None):
    """The release's SPECIFIC genres, most specific first, or None.

    Never raises. None means "no opinion": AI is not configured, the call
    failed, the answer could not be parsed, or nothing usable was left after
    canonicalization — the caller's own source list (which is at least ordered
    by source priority) is the better answer then.

    *count* is the track's total slot budget (`mb_genre_count`): at most
    `count - 1` SPECIFIC genres are asked for, because the first slot belongs
    to the derived family. A *count* of 1 has no room for a specific genre at
    all, so there is nothing to ask and the answer is None.

    *candidates* are the genres the sources already answered with, best first
    (the chain's merged order); *extra* is any further context worth naming in
    the prompt (release year, country, …).
    """
    try:
        count = max(1, int(count))
    except (TypeError, ValueError):
        count = DEFAULT_GENRE_COUNT
    # The family takes the first slot, so the model is asked for the ones
    # behind it — at most `count - 1`, and never more than the ceiling
    # mlo.genres enforces (a hand-edited config file cannot widen the ask).
    ask = min(GENRE_COUNT_MAX, count) - 1
    if ask < 1:
        return None
    cfg = _config()
    if not ai_client.ai_configured(cfg):
        return None
    try:
        prompt = _prompt(artist, album, title, track_path, candidates, ask,
                         extra, bool(cfg.get("ai_genre_research", True)))
    except Exception:
        return None
    path = _cache_file(prompt)
    # Any non-empty cached answer satisfies the question as asked (the ask
    # bounds the list, it does not require it to be full).
    hit = _cached(path, 1)
    if hit is not None:
        return hit
    cfg = dict(cfg)
    # The genre call has its own thinking budget; the client reads `ai_effort`
    # (script 17's key), so the genre knob is substituted in its place rather
    # than growing a second parameter on the shared client.
    cfg["ai_effort"] = cfg.get("ai_genre_effort") or cfg.get("ai_effort")
    try:
        text = ai_client.ai_chat(cfg, SYSTEM, prompt)
    except Exception:
        return None
    names = _specifics(_parse(text), candidates, ask)
    if not names:
        # Nothing usable: a refusal, prose, a family-only answer, or names the
        # model invented. The sources' own list stays as it was.
        return None
    _store(path, names)
    return names
