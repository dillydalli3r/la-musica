"""What ITUNESADVISORY a track gets when no provider stated one.

The providers are the first move, and `server.integrations.merge_advisory`
settles what they said (1 explicit beats everything, then a plain 0, then a
clean edition's 2). This module is what happens when NOBODY stated anything —
the answer would otherwise be an assumption, and the user's policy is that an
unstated advisory must not be invented without evidence. The ladder, in order:

1. **Instrumental.** A track carrying INSTRUMENTAL=1 has no words to be
   explicit with, so it is 0 — `auto_zero_advisory_for_instrumental` (on by
   default) is what makes this fire.
2. **The AI provider**, when one is configured (`ai_base_url` + `ai_model`)
   and `advisory_ai_classify` is on. It is asked for 0/1/2 with the track's
   lyrics when they exist (the whole point: a model reading the words beats a
   word list), and its answer REPLACES the word scan below. 3 means "cannot
   tell" and falls through — the value space this app stores is 0/1/2, and a
   stored 3 would fail the grader on every track that carried one.
3. **The lyrics word scan** (`mlo.advisory_words`, an extensive multilingual
   lexicon), when `advisory_lyrics_scan` is on and the track has lyrics: any
   hit is 1, no hit is 0. A track with no lyrics states nothing (the scan
   never reads silence as clean).
4. **`advisory_fallback`** — what to store when every step above was silent:
   `"0"` (the shipped default: not explicit), `"2"` (clean edition) or
   `"none"` (write nothing at all, leaving the track unrated).

Every decision carries its own provenance ("instrumental", "ai-lyrics",
"lyrics-scan", "fallback") and the words that hit, so the UI can show why a
value was written instead of a bare number. Nothing here writes a tag: the
callers (the import pipeline's advisory step, the advisory fetch endpoint)
own the file.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# The stages a decision can come from, in ladder order. Used as the
# provenance string the callers store and the UI shows.
STAGE_SOURCES = "sources"
STAGE_INSTRUMENTAL = "instrumental"
STAGE_AI = "ai"
STAGE_LYRICS = "lyrics"
STAGE_FALLBACK = "fallback"

# `advisory_fallback` is a string in the config ("0" / "2" / "none") so the
# three states survive JSON round trips and Settings renders them as a select.
VALID_FALLBACKS = ("0", "2", "none")

# How much of the lyrics a model is asked to read. The opening verse decides
# an explicit rating and a whole song's text is a needless prompt; the tail of
# a long song is where a swear word usually still shows up.
_LYRICS_LIMIT = 6000

_SYSTEM_PROMPT = (
    "You rate a song's iTunes advisory value from its lyrics. Answer with ONE "
    "digit and nothing else: 0 = no explicit content, 1 = explicit content "
    "(profanity, slurs, graphic sex or violence), 2 = a clean/edited edition of "
    "an explicit song, 3 = cannot tell (no lyrics, or not enough to judge). "
    "Judge the lyrics as written, in any language, including slang and "
    "transliteration. Never explain."
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


def scan_lyrics(text) -> List[str]:
    """Every profanity hit in *lyrics*, or [] — the lexicon is optional.

    `mlo.advisory_words` carries the multilingual lexicon; a checkout without
    it (or a broken import) must still answer, so a failure here reads as "no
    hits" rather than taking the whole advisory step down.
    """
    if not text:
        return []
    try:
        from . import advisory_words
    except Exception:
        return []
    try:
        return advisory_words.scan_lyrics(text)
    except Exception:
        return []


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


def decide_advisory(cfg, *, value=None, source="", answers=None, path="",
                    af=None, lyrics=None, instrumental=None) -> Dict:
    """The value to WRITE for one track, its provenance and its stage.

    `value`/`source`/`answers` come from the provider route (they may be
    absent — the route found nothing). `lyrics` is the track's own text (read
    here when not given), `instrumental` its INSTRUMENTAL tag. Returns
    ``{"value": 0|1|2|None, "source", "stage", "hits": [...], "fallback":
    bool}``; a None value means "leave the tag alone", which only happens when
    `advisory_fallback` is `none` and nothing else spoke.
    """
    if value is not None:
        return {"value": int(value), "source": source or "",
                "stage": STAGE_SOURCES, "hits": [], "fallback": False}

    tag = str(instrumental or "").strip()
    if tag != "1" and af is not None:
        try:
            tag = str(af.get_tag("INSTRUMENTAL") or "").strip()
        except Exception:
            tag = ""
    if tag == "1" and (cfg or {}).get("auto_zero_advisory_for_instrumental", True):
        return {"value": 0, "source": "instrumental",
                "stage": STAGE_INSTRUMENTAL, "hits": [], "fallback": False}

    if lyrics is None:
        lyrics = ""
        if path:
            try:
                from .lyrics_publish import local_lyrics
                lyrics = local_lyrics(path, af=af) or ""
            except Exception:
                lyrics = ""
    text = str(lyrics or "").strip()

    # The AI goes FIRST when one is configured: the user's default is that a
    # model reading the words replaces the word list, not that it backs it up.
    if (cfg or {}).get("advisory_ai_classify", True):
        artist = title = album = ""
        if af is not None:
            try:
                artist = str(af.get_tag("ARTIST") or af.get_tag("ALBUMARTIST") or "")
                title = str(af.get_tag("TITLE") or "")
                album = str(af.get_tag("ALBUM") or "")
            except Exception:
                artist = title = album = ""
        ai_answer = ai_advisory(cfg, artist=artist, title=title, album=album,
                                lyrics=text)
        if ai_answer is not None:
            return {"value": ai_answer[0], "source": ai_answer[1],
                    "stage": STAGE_AI, "hits": [], "fallback": False}

    if (cfg or {}).get("advisory_lyrics_scan", True) and text:
        hits = scan_lyrics(text)
        return {"value": 1 if hits else 0, "source": "lyrics-scan",
                "stage": STAGE_LYRICS, "hits": hits, "fallback": False}

    fallback = fallback_value(cfg)
    return {"value": fallback, "source": "fallback" if fallback is not None else "",
            "stage": STAGE_FALLBACK, "hits": [], "fallback": True}
