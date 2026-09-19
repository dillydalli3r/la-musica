"""AI genre ranking — the import's one model call.

`server.integrations.genre_chain` asks MusicBrainz, RateYourMusic and the
other configured sources first and hands their answers here; this module
turns them — plus, when ``ai_genre_research`` is on, the model's own
knowledge of the release — into the ``mb_genre_count`` genres in hierarchy
order the app stores (parent / main / sub, see ``mlo.genres``).

Optional end to end: no endpoint configured, a refusal, a timeout, an
unparseable answer or a list too short to be a hierarchy all mean ``None``,
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
from mlo.genres import DEFAULT_GENRE_COUNT, normalize_genres

SYSTEM = (
    "You are a music taxonomist naming the genres a release is filed under. "
    "You answer ONLY with the JSON object asked for."
)

# The shape the answer must have, spelled out with one worked example: the
# slots are the whole point of the feature (the grader checks them, see
# mlo.genres.issues), and a model left to itself answers with a flat bag of
# near-synonyms instead. Kept in the prompt rather than re-derived from the
# reply, because ordering is a judgement no word list reproduces.
_SLOTS = (
    "Answer with exactly {count} genres in hierarchy order — no numbering, "
    "no explanation, JSON only:\n"
    "  slot 1 the broad parent family (e.g. Rock, Hip Hop, Electronic),\n"
    "  slot 2 the main genre that applies to this release "
    "(e.g. Alternative Rock),\n"
    "  slot 3 the most specific subgenre or style it actually falls under "
    "(e.g. Post-Britpop).\n"
    "All genres must be distinct (never the same word twice, not even with "
    "different capitalisation) and every slot must be filled."
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


def infer_genres(*, artist, album, title="", track_path="", candidates=None,
                 count=DEFAULT_GENRE_COUNT, extra=None):
    """The release's genres in hierarchy order, or None to leave the sources'.

    Never raises. None means "no opinion": AI is not configured, the call
    failed, the answer could not be parsed, or it named fewer than two usable
    genres — with one genre there is no hierarchy to rank, so the caller's own
    list (which is at least ordered by source priority) is the better answer.

    *candidates* are the genres the sources already answered with, best first
    (the chain's merged order); *extra* is any further context worth naming in
    the prompt (release year, country, …).
    """
    try:
        count = max(1, int(count))
    except (TypeError, ValueError):
        count = DEFAULT_GENRE_COUNT
    cfg = _config()
    if not ai_client.ai_configured(cfg):
        return None
    try:
        prompt = _prompt(artist, album, title, track_path, candidates, count,
                         extra, bool(cfg.get("ai_genre_research", True)))
    except Exception:
        return None
    # The answer REPLACES the source list (see integrations._genre_ai_rank), so
    # it has to satisfy the same contract the sources did: `mb_genre_count`
    # genres, in order. A shorter answer used to be accepted (>= 2) and then
    # applied, which stored two genres on a track the grader requires three on
    # — a permanent failure, made permanent by the answer being disk-cached on
    # this prompt. So: fill every slot, or keep what the sources found.
    want = max(1, int(count or DEFAULT_GENRE_COUNT))
    path = _cache_file(prompt)
    hit = _cached(path, want)
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
    names = normalize_genres(_parse(text), count)
    # The model has to have done the job it was asked to do: a single name (or
    # prose that happened to look like one) is a refusal, not a ranking, and is
    # rejected the way the pre-inference chain rejected an empty answer. Two
    # slots of three IS a ranking — the model is right about the names it gave
    # and the rest of the hierarchy is filled from the fetched candidates,
    # because the stored list has to satisfy `mb_genre_count` or the grader
    # fails a track for a reason the user cannot fix (the answer is cached).
    if len(names) < min(2, want):
        return None
    if len(names) < want:
        names = normalize_genres(list(names) + list(candidates or []), count)
    if len(names) < want:
        return None
    _store(path, names)
    return names
