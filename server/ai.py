"""AI-assisted lyrics tooling.

All AI lyrics syncing / acoustic alignment was removed — the manual
editor's stamping and the deterministic ELRC builder below are the way
to sync. Remaining helpers:

* ``ai_chat`` - a minimal OpenAI-compatible /chat/completions client
  (works with OpenAI, OpenRouter, llama.cpp, oobabooga, LM Studio, and
  Google Gemini's OpenAI-compatible endpoint — a bare
  generativelanguage.googleapis.com base URL is auto-routed)
  configured through the ``ai_base_url`` / ``ai_api_key`` / ``ai_model``
  config keys.
* ``lyrics_clean`` / ``lyrics_repair`` - LLM-backed prompt wrappers that
  clean raw lyrics (ads, watermarks, layout garbage) and repair holes in
  imported lyrics using LRCLIB candidates.
* ``wordsync_lrc`` - deterministic (no AI) line→word/syllable sync: turns
  plain line-synced LRC into word-synced (or glued syllable-synced) ELRC
  by distributing times inside each line's slot, weighted by length.
"""
import os
import re
import threading

import httpx

from server.integrations import USER_AGENT

_GEMINI_HOST = "generativelanguage.googleapis.com"


def ai_config(config):
    """(base_url, api_key, model) from the flat config keys, normalized.

    Google's Gemini API speaks the OpenAI protocol on
    ``https://generativelanguage.googleapis.com/v1beta/openai`` — users
    typically paste just the bare host (or the full .../chat/completions
    endpoint), so both are routed to the correct base here."""
    base = str(config.get("ai_base_url") or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    if _GEMINI_HOST in base and not base.endswith("/openai"):
        base = f"https://{_GEMINI_HOST}/v1beta/openai"
    key = str(config.get("ai_api_key") or "").strip()
    model = str(config.get("ai_model") or "").strip()
    return base, key, model


def ai_configured(config):
    base, _key, model = ai_config(config)
    return bool(base and model)


def ai_effort(config):
    """Reasoning effort for AI calls: HIGH by default (cleanup, repair,
    translation and transliteration quality beat latency here). MINIMAL
    disables thinking entirely for speed."""
    raw = str(config.get("ai_effort") or "high").strip().lower()
    return raw if raw in ("minimal", "low", "medium", "high") else "high"


def ai_chat(config, system, user, timeout=90.0):
    """One-shot chat completion; returns the assistant message text."""
    base, key, model = ai_config(config)
    if not base or not model:
        raise ValueError("AI is not configured — set base URL and model in Settings → AI")
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    # Reasoning effort (OpenAI-style field; Google's OpenAI-compatible
    # endpoint maps it onto thinking budgets). Providers that reject the
    # unknown field get one plain retry.
    effort = ai_effort(config)
    if effort != "minimal":
        body["reasoning_effort"] = effort
    r = httpx.post(f"{base}/chat/completions", json=body, headers=headers,
                   timeout=timeout)
    if r.status_code >= 400 and "reasoning_effort" in body:
        body.pop("reasoning_effort")
        r = httpx.post(f"{base}/chat/completions", json=body, headers=headers,
                       timeout=timeout)
    if r.status_code >= 400:
        # surface the provider's own message (bad key, unknown model, …)
        detail = (r.text or "").strip().replace("\n", " ")[:300]
        raise ValueError(f"AI endpoint returned {r.status_code}: {detail}")
    data = r.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError(f"unexpected AI response shape: {str(data)[:200]}")
    return (content or "").strip()


CLEAN_SYSTEM = (
    "You clean up raw song lyrics. Remove advertising, download links, "
    "platform watermarks (e.g. 'Lyrics provided by...'), translator credits, "
    "editorial notes and duplicated blocks. Fix broken line wrapping so each "
    "line is a lyric phrase. Keep the original language, wording, spelling of "
    "words, section structure and line order. Never translate, summarize or "
    "add commentary. Output ONLY the cleaned lyrics, nothing else."
)


def lyrics_clean(config, text):
    """Clean raw (unsynced) lyrics via the configured LLM."""
    return ai_chat(config, CLEAN_SYSTEM, text)


REPAIR_SYSTEM = (
    "You repair corrupted song lyrics. You get the current lyrics and a list "
    "of candidate lines taken from other public sources of the same song. "
    "Fill missing lines, remove intruder lines and fix obviously wrong words "
    "using the candidates as evidence. Keep timestamps exactly where provided. "
    "Keep the original language and structure. Output ONLY the repaired lyrics "
    "in the same format (LRC if timestamps were given, plain text otherwise), "
    "nothing else."
)


def lyrics_repair(config, text, candidates, artist="", track=""):
    """Repair lyrics using LRCLIB candidate lines as evidence."""
    cand_text = "\n".join(
        f"- {c}" for c in candidates[:60] if c
    ) or "(no candidates)"
    header = f"Song: {artist} - {track}\n\n" if artist or track else ""
    user = f"{header}CURRENT LYRICS:\n{text}\n\nCANDIDATE LINES:\n{cand_text}"
    return ai_chat(config, REPAIR_SYSTEM, user)


# --------------------------------------------------------------------------- #
# Deterministic word sync (ELRC builder)
# --------------------------------------------------------------------------- #


def wordsync_lrc(lrc_text, level="word"):
    """Turn line-synced LRC into word- or syllable-synced ELRC.

    Delegates to mlo.lyrics.elrc_word_sync — the shared builder also powers
    the transliterate/translate script, so transformed lyrics carry the
    same kind of timings. level="syllable" glues per-syllable tags inside
    each word; CJK text sweeps per character (one kana = one syllable).
    """
    from mlo.lyrics import elrc_word_sync
    return elrc_word_sync(lrc_text, level=level)


# --------------------------------------------------------------------------- #
# Line-aligned transforms for the fullscreen player: translation and
# transliteration (romanization). Results are cached on disk per
# (mode, language, content) so a track is only processed once.
# --------------------------------------------------------------------------- #
import hashlib

def _cache_dir():
    """The lyrics-AI cache lives in <music folder>/.mlo/data too."""
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), "lyrics_ai_cache")

# Script 15 runs in a worker thread while the fullscreen player may be
# transforming the same track — serialize cache reads/writes so two writers
# can never interleave into the same JSON file.
_CACHE_LOCK = threading.Lock()

XLIT_SYSTEM = (
    "You romanize song lyrics. Convert every line from its original script "
    "(e.g. Japanese kana/kanji, Cyrillic, Hangul, Hanbi, Arabic, Devanagari) "
    "into Latin transliteration. Keep the same language, do NOT translate. "
    "Keep the original line count and order: exactly one output line per "
    "input line, same numbering. Output ONLY the transformed lines."
)

TRANSLATE_SYSTEM = (
    "You translate song lyrics. Translate every line into the requested "
    "target language. Keep the original line count and order: exactly one "
    "output line per input line, same numbering. Keep it singable and "
    "literal enough to follow along; do not add commentary. Output ONLY the "
    "translated lines."
)


def _cache_path(mode, lang, lines):
    h = hashlib.sha1(("|".join([mode, lang] + lines)).encode("utf-8")).hexdigest()
    return os.path.join(_cache_dir(), f"{mode}-{lang}-{h[:20]}.json")


def transform_lines(config, lines, mode, lang=""):
    """Translate ('translate') or transliterate ('transliterate') lyric
    lines, preserving line count. Disk-cached; raises ValueError when AI
    is not configured or the answer came back with no lines at all."""
    lines = [str(line) for line in lines]
    if not lines:
        return []
    base, key, model = ai_config(config)
    if not base or not model:
        raise ValueError("AI is not configured — set base URL and model in Settings → AI")
    if mode not in ("translate", "transliterate"):
        raise ValueError(f"unknown mode: {mode}")
    if not lang:
        lang = str(config.get("ai_translate_lang") or "en").strip() or "en"

    cache = _cache_path(mode, lang, lines)
    with _CACHE_LOCK:
        try:
            import json
            with open(cache, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            # an all-empty cached entry is a failed transform that must
            # never be re-served — ignore it and re-ask the model
            if (isinstance(cached, list) and len(cached) == len(lines)
                    and any(str(v).strip() for v in cached)):
                return cached
        except (OSError, ValueError):
            pass

    system = TRANSLATE_SYSTEM if mode == "translate" else XLIT_SYSTEM
    out = []
    import json
    CHUNK = 40
    for start in range(0, len(lines), CHUNK):
        chunk = lines[start:start + CHUNK]
        numbered = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(chunk))
        user = numbered
        if mode == "translate":
            user = f"Target language: {lang}\n\n{numbered}"
        text = ai_chat(config, system, user)
        got = [re.sub(r"^\s*\d+\.\s*", "", ln).strip()
               for ln in text.splitlines() if ln.strip()]
        # line-count repair: the model must echo one line per input line
        while len(got) < len(chunk):
            got.append("")
        if len(got) > len(chunk):
            got = got[:len(chunk)]
        if not any(got) and any(line.strip() for line in chunk):
            # refusal / truncated / timeout answer: an all-blank transform
            # would blank every lyric line, so fail instead of storing it
            raise ValueError("AI returned no lines")
        out.extend(got)

    # Never persist an all-blank result: it would be re-served forever.
    if any(str(v).strip() for v in out):
        with _CACHE_LOCK:
            try:
                os.makedirs(_cache_dir(), exist_ok=True)
                import json
                with open(_cache_path(mode, lang, lines), "w", encoding="utf-8") as fh:
                    json.dump(out, fh, ensure_ascii=False)
            except OSError:
                pass
    return out


