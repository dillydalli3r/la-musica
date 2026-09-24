"""AI-assisted lyric transforms — the one optional model in the app.

Lyric transliteration and translation (SCRIPT 17, ``mlo.lyrics_xlit``) run
through here. Everything else in the library is deterministic; nothing in
this module is ever required for the app to work, and the script that uses
it no-ops with one log line when the keys below are unset.

* ``ai_chat`` - a minimal OpenAI-compatible /chat/completions client
  (works with OpenAI, OpenRouter, llama.cpp, oobabooga, LM Studio, and
  Google Gemini's OpenAI-compatible endpoint — a bare
  generativelanguage.googleapis.com base URL is auto-routed)
  configured through the ``ai_base_url`` / ``ai_api_key`` / ``ai_model``
  config keys.
* ``transform_lines`` - line-aligned translation / romanization of a lyric
  text, disk-cached per (mode, language, content) so a track is only paid
  for once and a re-run costs nothing.
"""
import hashlib
import json
import os
import re
import threading

import httpx

# Importing it installs the ONE shared client behind the module-level
# `httpx.post` the chat call uses (server/httpclient.py).
from server import httpclient  # noqa: F401

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
    """Reasoning effort for AI calls: HIGH by default — translation and
    transliteration quality beat latency here. MINIMAL disables thinking
    entirely for speed; MAX asks for the provider's own ceiling, which
    `ai_chat` reaches by dropping a rung at a time (a compatible endpoint that
    does not know the word falls back to HIGH before the field is dropped)."""
    raw = str(config.get("ai_effort") or "high").strip().lower()
    return raw if raw in ("minimal", "low", "medium", "high", "max") else "high"


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
    # endpoint maps it onto thinking budgets). The attempts are a LADDER, not
    # one value plus a plain retry: MAX is the user asking for the provider's
    # own ceiling, and not every compatible endpoint knows that word — so a
    # rejection drops to HIGH before the field itself is dropped, which is
    # "the highest this provider accepts" rather than "its default". Every
    # other value behaves as it always did: the value, then no field at all.
    effort = ai_effort(config)
    attempts = [None] if effort == "minimal" else (
        ["max", "high", None] if effort == "max" else [effort, None]
    )
    r = None
    for value in attempts:
        if value is None:
            body.pop("reasoning_effort", None)
        else:
            body["reasoning_effort"] = value
        r = httpx.post(f"{base}/chat/completions", json=body, headers=headers,
                       timeout=timeout)
        if r.status_code < 400:
            break
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


# --------------------------------------------------------------------------- #
# Line-aligned transforms: translation and transliteration (romanization).
# Results are cached on disk per (mode, language, content) so a track is only
# processed once — script 17 and any other caller share the same cache.
# --------------------------------------------------------------------------- #

# Script 17 runs in a worker thread; serialize cache reads/writes so two
# writers can never interleave into the same JSON file.
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


def _cache_dir():
    """The lyrics-AI cache lives in the app's state folder."""
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), "lyrics_ai_cache")


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
    base, _key, model = ai_config(config)
    if not base or not model:
        raise ValueError("AI is not configured — set base URL and model in Settings → AI")
    if mode not in ("translate", "transliterate"):
        raise ValueError(f"unknown mode: {mode}")
    if not lang:
        # The configured target languages, not the never-written
        # `ai_translate_lang`: `lyrics_translation_langs` is the key the
        # transliteration settings own ("en,de"), so the first one is what a
        # bare translate call means.
        langs = str(config.get("lyrics_translation_langs") or "en").strip()
        lang = (langs.split(",")[0].strip() or "en")

    cache = _cache_path(mode, lang, lines)
    with _CACHE_LOCK:
        try:
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
        # A model that echoes the directive instead of answering it would
        # shift every line by one and store a transform that no longer lines
        # up with the lyrics — drop that line rather than accept the shift.
        if mode == "translate" and got:
            directive = f"target language: {lang}".lower()
            if got[0].strip().lower() == directive:
                got.pop(0)
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
                with open(_cache_path(mode, lang, lines), "w", encoding="utf-8") as fh:
                    json.dump(out, fh, ensure_ascii=False)
            except OSError:
                pass
    return out


# --------------------------------------------------------------------------- #
# Which language a track's lyrics are in (script 17's own question).
# --------------------------------------------------------------------------- #
LANGUAGE_SYSTEM = (
    "You identify the language song lyrics are written in. Answer with ONE "
    "ISO 639-1 code — two lower-case letters, e.g. en, de, ja, fr — and "
    "nothing else. Answer exactly \"und\" when the text is not in one "
    "language, is a transliteration of another language, or is too short to "
    "tell."
)

# How much of a lyric text the question needs: a verse states the language as
# surely as the whole song, and the ask stays cheap on a 40-line chorus.
_LANG_LIMIT = 1200


def _lang_code(text):
    """A model's answer as a bare language code, "" when it did not name one.

    Deliberately strict — a wrong code is written into the track's own LANGUAGE
    tag and then read back by grading, so a guess is worse than no answer. The
    accepted shapes are the ones a model really produces for this question:
    a bare code (``ja``), a trailing parenthesised one (``Japanese (ja)``), and
    a leading one followed by a separator (``ja — Japanese``). "und" and the
    other codes that state nothing (`mlo.lyrics_xlit.normalize_lang`) are "".
    """
    from mlo.lyrics_xlit import _MB_LANG, normalize_lang

    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    m = re.fullmatch(r"[`\"']?([A-Za-z]{2,3})[`\"']?\.?", raw)
    if m is None:
        m = (re.search(r"\(([A-Za-z]{2,3})\)", raw)
             or re.match(r"([A-Za-z]{2,3})\s*[-–—:,]", raw))
    if m is None:
        return ""
    code = m.group(1).lower()
    if len(code) == 3 and code not in _MB_LANG:
        # A three-letter token that is not a known ISO 639-3 code is a word
        # ("the", "und") rather than an answer.
        return ""
    return normalize_lang(code)


def detect_language(config, text):
    """The language one lyric text is written in, as a bare code ("" when the
    model cannot say) — disk-cached per text, so a track is asked about once.

    The question the app cannot always answer itself: script and function words
    separate most languages, and the two they cannot (`mlo.lyrics_xlit
    .detect_language` — two Latin-script languages with no shared stopwords) are
    exactly what a model reads reliably. The answer's caller WRITES it into the
    track's LANGUAGE tag, so grading reads a stored fact and never asks a model
    anything, and the next run's question is already answered.
    """
    body = str(text or "").strip()
    if not body:
        return ""
    base, _key, model = ai_config(config)
    if not base or not model:
        return ""
    lines = [body[:_LANG_LIMIT]]
    cache = _cache_path("language", "", lines)
    with _CACHE_LOCK:
        try:
            with open(cache, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if isinstance(cached, list) and cached:
                # A remembered NON-answer ("" / "und") is served too: it cost a
                # request, and asking the same text again is the same answer.
                return str(cached[0] or "")
        except (OSError, ValueError):
            pass
    code = _lang_code(ai_chat(config, LANGUAGE_SYSTEM, lines[0]))
    with _CACHE_LOCK:
        try:
            os.makedirs(_cache_dir(), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump([code], fh, ensure_ascii=False)
        except OSError:
            pass
    return code
