"""What ITUNESADVISORY a track gets: the providers, the AI, and the ladder.

The providers are the first move, and `server.integrations.merge_advisory`
settles what they said (1 explicit beats everything, then a plain 0, then a
clean edition's 2). The AI provider is a SOURCE of that merge now (issue #28):
it is asked once for every track that is not already settled by its own
INSTRUMENTAL tag, whenever one is configured (`ai_base_url` +
`ai_model`) and `advisory_ai_classify` is on, and its answer is ranked against
the providers' by the same rule — a stated 1 survives anything the AI says, an
AI 1 overrules a stated 0 or 2, and an AI 2 never overrules a stated 0. It only
overrules a stated value when it read the track's words: a model shown "(none
available)" has read nothing, and nothing is not the evidence it takes to
contradict a provider. Its answer is recorded either way, so the reply's
provenance chips can name it beside the providers.

This module is also what happens when NOBODY stated anything — the answer
would otherwise be an assumption, and the user's policy is that an unstated
advisory must not be invented without evidence. The ladder, in order:

1. **Instrumental.** A track carrying INSTRUMENTAL=1 has no words to be
   explicit with, so it is 0 — `auto_zero_advisory_for_instrumental` (on by
   default) is what makes this fire.
2. **The AI provider**, asked for 0/1/2 about the song's SUBJECT AND ATTITUDE
   with its lyrics when they exist (the whole point: a model reading the whole
   song beats a word list — a mild word in passing is not explicit, and the
   rubric in `_SYSTEM_PROMPT` says so), and its answer REPLACES the word scan
   below. 3 means "cannot tell" and falls through — the value space this app
   stores is 0/1/2, and a stored 3 would fail the grader on every track that
   carried one.
3. **The lyrics word scan** (`mlo.advisory_words`, an extensive multilingual
   lexicon), when `advisory_lyrics_scan` is on and the track has lyrics: only a
   hit from the lexicon's STRONG set is 1. The mild tier ("ass", "arse",
   "culo") is reported in `hits` and decides nothing — a lone "ass" is not the
   evidence "motherfucker" is — so a track whose only hits are mild is 0. A
   track with no lyrics states nothing (the scan never reads silence as clean).
4. **`advisory_fallback`** — what to store when every step above was silent:
   `"0"` (the shipped default: not explicit), `"2"` (clean edition) or
   `"none"` (write nothing at all, leaving the track unrated).

The ladder also runs the other way: when a provider DID state 0, the stages
that read the words are still consulted. The providers miss exactly there
(Deezer's `explicit_lyrics: false` covers "not classified", Apple's
`notExplicit` is the master's own flag on a track whose words are explicit), so
a stated 0 that the words contradict escalates to 1 and the SOURCE names the
signal that overruled the provider ("lyrics-scan (escalated)"). The scan's job
there is explicit-ONLY and one-way: it never turns a stated 1, or a clean
edition's 2, into anything else, and it escalates on a strong hit alone — mild
language is not a contradiction, so a stated 0 the mild tier agrees with stays
0. The AI is not a signal but a source — the rank above decides between its
answer and the providers' — so it is the one stage that can overrule a stated 2.

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

# What an ESCALATED value's provenance says. Every other source names WHO
# stated the value; this one names the signal that OVERRULED a provider —
# "lyrics-scan (escalated)" — because the stored 1 is the opposite of what the
# provider said. A reader that saw a bare provider name beside it would credit
# that provider with a rating it never gave.
ESCALATED = " (escalated)"

# How much of the lyrics a model is asked to read. The rubric asks for the
# song's SUBJECT AND TONE, and a theme can be carried by the last verse as much
# as the first — so the cap is a safety valve against a pathological file (a
# whole discography in one .lrc), not a judgement budget: 12000 characters is
# past the end of all but the longest lyrics, and a text that runs over it is
# cut rather than refused.
_LYRICS_LIMIT = 12000

# The rating rubric. It asks for the song's SUBJECT AND ATTITUDE, not for a
# keyword count: the failure this prompt exists to prevent is a track marked
# explicit because one mild word appears in it. Which terms are mild enough to
# say so is the lexicon's own tier (`mlo.advisory_words.MILD`), and the two
# must agree — the examples below are that tier's English entries, and the
# prompt's rule ("mild in passing is not explicit") is that tier's rule stated
# for a reader that has no lexicon.
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


def _strong_hits(hits) -> List[str]:
    """The hits that establish explicit: the lexicon's mild tier filtered out.

    `scan_lyrics` reports EVERY term it saw, and the mild entries in that list
    ("ass", "arse", "culo") are evidence of nothing on their own — a song that
    carries one in passing is not explicit for it (see
    `mlo.advisory_words.MILD`). The filter lives there, in the data, so "which
    terms count" is declared once and a caller that asked `if hits` cannot
    disagree with it. No lexicon means no hits to filter either.
    """
    if not hits:
        return []
    try:
        from . import advisory_words
        return advisory_words.strong(hits)
    except Exception:
        return list(hits)


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
    `auto_zero_advisory_for_instrumental` on is the ladder's FIRST step, which
    is why the one question is asked in ONE place — a track settled by its own
    tag is settled before anything is consulted about it.
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

    The ONE place the model is spoken to, because issue #28 makes the call
    unconditional: the provider is a source of its own now, asked however the
    network sources answered, and every consumer of the answer shares this
    verdict — `_word_stages` when nobody stated anything, `_stated` when a
    provider did. An N-track album therefore costs N calls, never one per stage
    that wants the answer.

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
            "hits": [], "fallback": False}


def _outranks(candidate, value) -> bool:
    """True when the AI's answer *candidate* beats the value the providers stated.

    `server.integrations.merge_advisory` is the app's ONE rank for
    ITUNESADVISORY (1 beats 0 beats 2), and it is the merge the providers' own
    answers already went through — so the AI's answer is weighed by ASKING it
    again over `{stated, candidate}` rather than by a second copy of the order
    living here: one rule, one place, and the day that rule changes the AI
    moves with the providers. It is imported lazily because `mlo/` runs
    without `server/` in the CLI, where there is no provider map to join
    either: a value outside 0/1/2 (a hand-edited tag, a caller's junk)
    outranks nothing, and neither does anything when the merge is unavailable.
    """
    if candidate not in (0, 1, 2) or value not in (0, 1, 2) or candidate == value:
        return False
    try:
        from server import integrations
    except Exception:
        return False
    merged = integrations.merge_advisory({"providers": value, "ai": candidate})
    return merged == candidate


def _word_stages(cfg, *, text, verdict) -> Optional[Dict]:
    """The two stages that READ the words: the AI, then the word scan.

    The AI goes FIRST — the user's default is that a model reading the words
    replaces the word list, not that it backs it up — and `verdict` is the
    answer it already gave (`_ai_verdict`), so the one call per track is shared
    with the stated path (`_stated`) instead of paid for twice. An answer it
    DID give is this stage's answer, and the scan is never consulted over it.
    The scan is the second move and answers 1 on any term, 0 on none. None
    means neither stage could answer at all — both switched off, and no text
    for the scan — which is what leaves `advisory_fallback` as the last resort.
    """
    if verdict is not None:
        return verdict
    if (cfg or {}).get("advisory_lyrics_scan", True) and text:
        hits = scan_lyrics(text)
        # The mild tier is reported, never decisive: a lyric whose only hits
        # are "ass" or "culo" is 0 like any other clean track, and the reply
        # still names what was seen (`hits`) so a reader can check the call.
        return {"value": 1 if _strong_hits(hits) else 0, "source": "lyrics-scan",
                "stage": STAGE_LYRICS, "hits": hits, "fallback": False}
    return None


def _escalate_explicit(cfg, text) -> Optional[Dict]:
    """A provider's stated 0 overruled by the word scan, or None.

    The providers miss on explicit content, and they miss in the same
    direction: Deezer's `explicit_lyrics: false` also covers "not classified"
    and Apple's `trackExplicitness: notExplicit` is the MASTER's own flag —
    Apple ships an explicit track under it (`tools/test_advisory_sources.py`'s
    real Steal This Album payload marks only five of sixteen tracks explicit,
    and the app matched those five). A stated 0 therefore does not close the
    question, and the scan is the signal consulted for it — explicit-ONLY,
    because this stage has no way to say "clean edition".

    Only a STRONG hit escalates — the mild tier is reported and nothing more,
    so a stated 0 the words agree with (only "ass" in the lyric, say) stays 0,
    which is `None` here ("no escalation"), not a new decision. The track's
    words must EXIST for the escalation to fire at all: there is nothing to
    read, and nothing is not the evidence it takes to contradict a provider.
    The returned decision names the signal that fired (`ESCALATED`), never the
    provider it overruled.
    """
    if not text or not (cfg or {}).get("advisory_lyrics_scan", True):
        return None
    hits = scan_lyrics(text)
    if not _strong_hits(hits):
        return None
    return {"value": 1, "source": "lyrics-scan" + ESCALATED,
            "stage": STAGE_LYRICS, "hits": hits, "fallback": False}


def _stated(cfg, value, source, *, text, verdict) -> Dict:
    """What to write when a provider stated *value*, once the AI has spoken.

    The AI NEVER overrules a provider (the owner's rule): a value another
    source stated is written as it stands, with that source's provenance. The
    model is a BACKUP — asked once per track, used where no provider answered
    (see `_word_stages`) — not a second opinion that can lower a rating the
    sources already agreed on. Issue #28 made it a source competing by
    `merge_advisory`'s rank (an AI 1 outranking a stated 0 or a clean edition's
    2); that is the behaviour this function no longer implements.

    A stated 0 still falls through to the word scan (`_escalate_explicit`), the
    explicit-only signal for exactly that case: the scan reads the track's own
    words and is not the model. A stated 1 or 2 is returned as it stands.
    """
    value = int(value)
    if verdict is None:
        if value == 0:
            escalated = _escalate_explicit(cfg, text)
            if escalated is not None:
                return escalated
    if value == 0:
        escalated = _escalate_explicit(cfg, text)
        if escalated is not None:
            return escalated
    return {"value": value, "source": source or "",
            "stage": STAGE_SOURCES, "hits": [], "fallback": False}


def decide_advisory(cfg, *, value=None, source="", answers=None, path="", af=None,
                    lyrics=None, instrumental=None) -> Dict:
    """The value to WRITE for one track, its provenance and its stage.

    `value`/`source` come from the provider route (they may be absent — the
    route found nothing) and `answers` is that route's per-source map, which
    the AI's own answer is recorded into: the reply reports one map, and the
    UI's provenance chips name the sources behind the value from it. `lyrics`
    is the track's own text (read here when not given), `instrumental` its
    INSTRUMENTAL tag. Returns ``{"value": 0|1|2|None, "source", "stage",
    "hits": [...], "fallback": bool}``; a None value means "leave the tag
    alone", which only happens when `advisory_fallback` is `none` and nothing
    else spoke.

    The AI is asked ONCE, for every track that reaches this function (issue
    #28: it is a source, not a last resort), and its answer is ranked against
    what the providers said by the app's own rule — 1 beats 0 beats 2, the one
    `server.integrations.merge_advisory` applies. `_stated` is that comparison
    when a provider stated something; the ladder (`_word_stages`) is what
    decides when nobody did. An INSTRUMENTAL track is settled by its own tag
    before anything is asked: it is 0 under the shipped rule, or whatever a
    provider stated, and a word-reading stage has nothing to say about a track
    with no words — so the call whose answer no branch could use is not made.
    """
    if _is_instrumental(cfg, af, instrumental):
        if value is None:
            return {"value": 0, "source": "instrumental",
                    "stage": STAGE_INSTRUMENTAL, "hits": [], "fallback": False}
        return {"value": int(value), "source": source or "",
                "stage": STAGE_SOURCES, "hits": [], "fallback": False}

    text = _lyrics_text(path, af, lyrics)
    verdict = _ai_verdict(cfg, af=af, lyrics=text)

    if value is not None:
        # A provider stated something: written as it stands, AI or not — and
        # the model's answer is NOT recorded as a source behind it, because it
        # is not one.
        return _stated(cfg, value, source, text=text, verdict=verdict)
    if verdict is not None and answers is not None:
        # No provider spoke, so the model's answer is what the reply reports.
        answers[str(verdict.get("source") or STAGE_AI)] = verdict["value"]

    out = _word_stages(cfg, text=text, verdict=verdict)
    if out is not None:
        return out

    fallback = fallback_value(cfg)
    return {"value": fallback, "source": "fallback" if fallback is not None else "",
            "stage": STAGE_FALLBACK, "hits": [], "fallback": True}
