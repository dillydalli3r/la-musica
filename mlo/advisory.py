"""What ITUNESADVISORY a track gets: the providers, the AI, and the ladder.

The providers are the first move, and `server.integrations.merge_advisory`
settles what they said (1 explicit beats everything, then a plain 0, then a
clean edition's 2). A provider that STATES a value ends the question: it is
written as it stands with that provider's provenance, and NOTHING else is asked
about it — the AI included. A stated 0 is FINAL, never re-opened by a stage
below it, and the AI is never asked to second-guess a source that already
answered.

This module is also what happens when NOBODY stated anything — the answer would
otherwise be an assumption, and the user's policy is that an unstated advisory
must not be invented without evidence. The ladder, in order:

1. **The instrumental rule.** A track carrying INSTRUMENTAL=1 has no words to
   be explicit with, so it is 0 — `auto_zero_advisory_for_instrumental` (on by
   default) is what makes this fire.
2. **The AI provider**, asked for 0/1/2 about the song's SUBJECT AND ATTITUDE,
   with its lyrics when they exist (the rubric in `_SYSTEM_PROMPT` reads them).
   It is asked ONLY here — when every source above came up with nothing at all
   — and never to second-guess one that stated something: the model is what
   answers the unanswerable, not a competing opinion. 3 means "cannot tell" and
   falls through — the value space this app stores is 0/1/2, and a stored 3
   would fail the grader on every track that carried one.
3. **`advisory_fallback`** — what to store when every step above was silent:
   `"0"` (the shipped default: not explicit), `"2"` (clean edition) or
   `"none"` (write nothing at all, leaving the track unrated).

Every decision carries its own provenance ("instrumental", "ai-lyrics", "ai", a
provider id, "fallback"), so the UI can show why a value was written instead of
a bare number. Nothing here writes a tag: the callers (the import pipeline's
advisory step, the advisory fetch endpoint) own the file.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

# The stages a decision can come from, in ladder order. Used as the
# provenance string the callers store and the UI shows.
STAGE_SOURCES = "sources"
STAGE_INSTRUMENTAL = "instrumental"
STAGE_AI = "ai"
STAGE_FALLBACK = "fallback"

# `advisory_fallback` is a string in the config ("0" / "2" / "none") so the
# three states survive JSON round trips and Settings renders them as a select.
VALID_FALLBACKS = ("0", "2", "none")

# How much of the lyrics a model is asked to read. The rubric asks for the
# song's SUBJECT AND TONE, and a theme can be carried by the last verse as much
# as the first — so the cap is a safety valve against a pathological file (a
# whole discography in one .lrc), not a judgement budget: 12000 characters is
# past the end of all but the longest lyrics, and a text that runs over it is
# cut rather than refused.
_LYRICS_LIMIT = 12000

# The rating rubric. It asks for the song's SUBJECT AND ATTITUDE, not for a
# keyword count: the failure this prompt exists to prevent is a track marked
# explicit because one mild word appears in it. "A lone 'ass', 'damn' or
# 'hell' … is not explicit on its own" is the rule the model is given, and it
# is why a reading of the song is what this ladder asks for when the sources
# came up empty.
_SYSTEM_PROMPT = (
    "You rate a song's iTunes advisory value from what the song is ABOUT, not "
    "from the words it happens to contain. Answer with ONE digit and nothing "
    "else: 0 = not explicit, 1 = explicit, 2 = a clean/edited edition of an "
    "explicit song, 3 = cannot tell (no lyrics, or not enough to judge). "
    "Rate 1 when the lyrics are explicit as a whole — profanity that is "
    "excessive, or a slur, or a very strong word (hard swearing, a sexual or "
    "violent term of abuse), or graphic sex, violence or drug use. Rate 0 "
    "when they are not, INCLUDING a song that is otherwise clean and carries "
    "a mild word in passing: a lone 'ass', 'damn' or 'hell', an idiom, a word "
    "quoted from someone else, or a word that is ordinary in another language "
    "is not explicit on its own. Weigh the theme and the tone over the "
    "vocabulary — a song about heartbreak that swears once is not the same "
    "record as one whose subject is the profanity. The lyrics may be in ANY "
    "language or script: judge them in that language, slang, transliteration "
    "and mixed-language lines included, and never answer 3 merely because the "
    "lyrics are not English. Never explain."
)


def fallback_value(cfg) -> Optional[int]:
    """`advisory_fallback` as 0/2, or None for "write nothing"."""
    raw = str((cfg or {}).get("advisory_fallback", "0")).strip().lower()
    if raw in ("none", "off", "", "null"):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value in (0, 2) else 0


def ai_advisory(cfg, artist="", title="", album="", lyrics="") -> Optional[Tuple[int, str]]:
    """(value, source) from the configured AI provider, or None.

    `source` is "ai-lyrics" when the model was given the words and "ai" when
    it was not (no lyrics in the file), which is what the provenance readout
    shows. An unconfigured, unreachable or nonsensical answer returns None —
    the caller then falls through the ladder, never blocks on the network.
    """
    try:
        from server import ai  # lazy: mlo/ runs without server/ in the CLI
    except Exception:
        return None
    try:
        if not ai.ai_configured(cfg):
            return None
    except Exception:
        return None
    text = str(lyrics or "").strip()
    user = f"Artist: {artist or 'unknown'}\nTitle: {title or 'unknown'}\nAlbum: {album or 'unknown'}\n"
    user += ("Lyrics:\n" + text[:_LYRICS_LIMIT]) if text else "Lyrics: (none available)"
    try:
        reply = ai.ai_chat(cfg, _SYSTEM_PROMPT, user, timeout=60.0)
    except Exception:
        return None
    digit = ""
    for char in str(reply or ""):
        if char.isdigit():
            digit = char
            break
    if digit not in ("0", "1", "2"):
        # 3 (or unparseable) = the model declined to rate: fall through.
        return None
    return int(digit), ("ai-lyrics" if text else "ai")


def _lyrics_text(path="", af=None, lyrics=None) -> str:
    """The track's own words: the caller's when given, else off the disk."""
    if lyrics is not None:
        return str(lyrics or "").strip()
    if not path:
        return ""
    try:
        from .lyrics_publish import local_lyrics
        return str(local_lyrics(path, af=af) or "").strip()
    except Exception:
        return ""


def _is_instrumental(cfg, af, instrumental) -> bool:
    """True when the track is an instrumental under the shipped rule.

    The tag may be handed in or read off the file; `INSTRUMENTAL=1` with
    `auto_zero_advisory_for_instrumental` on is the ladder's first move once no
    source stated a value, which is why the one question is asked in ONE place —
    a track settled by its own tag is settled before the AI is consulted about
    it, and a track a provider already rated is never asked about at all.
    """
    tag = str(instrumental or "").strip()
    if tag != "1" and af is not None:
        try:
            tag = str(af.get_tag("INSTRUMENTAL") or "").strip()
        except Exception:
            tag = ""
    return tag == "1" and (cfg or {}).get("auto_zero_advisory_for_instrumental", True)


def _ai_verdict(cfg, af=None, lyrics="") -> Optional[Dict]:
    """The AI provider's answer for one track, as a ladder decision, or None.

    The ONE place the model is spoken to, and it is reached from ONE place too:
    `decide_advisory` calls it only when NO source stated a value and the track
    is not an instrumental — the model answers what nobody else could, and is
    never asked to second-guess a source that already answered. One call per
    track that gets this far, never one per stage.

    It is asked even when the file carries no lyrics (the song it knows is
    evidence too, which is why its source reads "ai" rather than "ai-lyrics").
    None means it said nothing usable — switched off, not configured, no reply,
    or an answer that is not a rating (3 = "cannot tell": the value space this
    app stores is 0/1/2, and a stored 3 would fail the grader on every track
    carrying one).
    """
    if not (cfg or {}).get("advisory_ai_classify", True):
        return None
    artist = title = album = ""
    if af is not None:
        try:
            artist = str(af.get_tag("ARTIST") or af.get_tag("ALBUMARTIST") or "")
            title = str(af.get_tag("TITLE") or "")
            album = str(af.get_tag("ALBUM") or "")
        except Exception:
            artist = title = album = ""
    answer = ai_advisory(cfg, artist=artist, title=title, album=album,
                         lyrics=lyrics)
    if answer is None:
        return None
    return {"value": answer[0], "source": answer[1], "stage": STAGE_AI,
            "fallback": False}


def decide_advisory(cfg, *, value=None, source="", answers=None, path="", af=None,
                    lyrics=None, instrumental=None) -> Dict:
    """The value to WRITE for one track, its provenance and its stage.

    `value`/`source` come from the provider route (they may be absent — the
    route found nothing) and `answers` is that route's per-source map, which the
    AI's own answer is recorded into: the reply reports one map, and the UI's
    provenance chips name the sources behind the value from it. `lyrics` is the
    track's own text (read here when the AI is the stage that needs it),
    `instrumental` its INSTRUMENTAL tag. Returns ``{"value": 0|1|2|None,
    "source", "stage", "fallback": bool}``; a None value means "leave the tag
    alone", which only happens when `advisory_fallback` is `none` and nothing
    else spoke.

    The ladder is asked in ONE direction and stops at its first answer:

    1. A provider that STATED a value wins outright. It is written as it
       stands, with that provider's provenance, and nothing else is consulted
       — no instrumental rule, no AI. A stated 0 is final: the sources miss
       explicit content in their own direction (Deezer's `explicit_lyrics:
       false` also covers "not classified", Apple's `notExplicit` is the
       master's own flag), but re-opening a stated value is not this ladder's
       job any more, and an answer that contradicts a source is exactly the
       second opinion the user had removed.
    2. The instrumental rule: no source spoke, and the track carries
       INSTRUMENTAL=1, so it is 0 under `auto_zero_advisory_for_instrumental`.
       An instrumental has no words to be explicit with, and costs no AI call.
    3. The AI (`_ai_verdict`), asked only now — every source came up with
       nothing at all — and fed the track's words when they exist. Its 0/1/2
       is the answer; 3 or garbage is not, and falls through.
    4. `advisory_fallback`, the last resort.
    """
    if value is not None:
        return {"value": int(value), "source": source or "",
                "stage": STAGE_SOURCES, "fallback": False}

    if _is_instrumental(cfg, af, instrumental):
        return {"value": 0, "source": "instrumental",
                "stage": STAGE_INSTRUMENTAL, "fallback": False}

    text = _lyrics_text(path, af, lyrics)
    verdict = _ai_verdict(cfg, af=af, lyrics=text)
    if verdict is not None:
        # No provider spoke, so the model's answer is what the reply reports as
        # the value's source, beside whatever the providers said (nothing).
        if answers is not None:
            answers[str(verdict.get("source") or STAGE_AI)] = verdict["value"]
        return verdict

    fallback = fallback_value(cfg)
    return {"value": fallback, "source": "fallback" if fallback is not None else "",
            "stage": STAGE_FALLBACK, "fallback": True}
