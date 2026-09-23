"""Script 17 — Lyrics Transliterate & Translate (AI).

Makes translations and romanizations durable library data: for every track
with lyrics it

* transliterates non-Latin lyrics into Latin script (romanization). Lyrics
  that are already Latin script are treated as their own transliteration —
  nothing is written for them (romanizing Spanish would be a no-op), and a
  reader whose own language is written in that script is served too (a ja
  reader does not need Japanese lyrics romanized);
* translates the lyrics into every language configured in
  ``lyrics_translation_langs`` (comma separated, e.g. "en,de") — unless the
  lyrics are already in the first of them, which is the reader's own
  language;

and decides BOTH in one place, ``xlit_needs`` below: it is asked before
anything is written and the grader asks it before judging what is stored, so
the two can never disagree — a re-run of an already-correct library writes
nothing at all, and a transform the rule no longer asks for (one written
under an older, laxer rule) is DELETED rather than left to fail grading
forever.

Results are stored in two places:

* embedded tags, one per language — ``TRANSLITERATION-JA-LATN`` (romanized
  Japanese), ``TRANSLATION-EN``, ``TRANSLATION-DE``, … written via the
  arbitrary-tag path of every container (TXXX / freeform atoms / vorbis
  comments). The bare legacy ``TRANSLITERATION`` / ``TRANSLATION`` names
  are superseded and removed when the language-specific tag is written;
* LRC sidecars ``<stem>.romaji.lrc`` and ``<stem>.<lang>.lrc`` — the
  de-facto convention players use for translated karaoke files. These are
  written ONLY when the configured lyrics format is LRC or BOTH: with
  EMBEDDED lyrics the transforms live in the tags alone, never in stray
  files next to the audio (gated further by ``lyrics_xlit_sidecars``).

Line and timestamp structure of the original lyrics is preserved exactly:
for synced (LRC/ELRC) lyrics every output line keeps its original
``[mm:ss.xx]`` prefix and its ``<mm:ss.xx>`` tags are re-distributed across
the transformed words at the configured ``lrc_sync_level`` (per character
for CJK), so romanized and translated lyrics stay syllable/word-synced
karaoke — and blank lines pass through untouched, so the stored transforms
stay line-aligned with the original and can be rendered without
re-alignment. The player shows these stored transforms as sub-lines; it
generates nothing itself.

The AI itself comes from the shared OpenAI-compatible client in
``server/ai`` (imported lazily so the plain CLI still works); results are
disk-cached there per (mode, language, content), so re-runs only pay for
new or changed lyrics. With no AI configured the script logs one line and
returns — an unconfigured app never fails a run over it.
"""
import os
import re
import unicodedata

from .audio import AudioFile
from .lyrics import _atomic_write_text, _lrc_for, elrc_word_sync
from .paths import AUDIO_EXTS
from .stats import (
    is_audio_file, new_stats, _collect_targets, _find_albums,
    _make_pbar, _pbar_skip, _pbar_update, worker_count,
)
from .ui import print_header, log, c, Color

# Leading [mm:ss.xx] timestamps of an LRC line (possibly several for
# multiple synced copies of the same line).
_LINE_TS_RE = re.compile(r"^((?:\[\d{1,2}:\d{1,2}(?:[.:]\d{1,3})?\])+)")
# Word-level <mm:ss.xx> tags (ELRC). Not re-generated for transformed text.
_WORD_TAG_RE = re.compile(r"<\d{1,2}:\d{1,2}(?:[.:]\d{1,3})?>")
# LRC metadata headers ("[ar:Artist]", "[offset:+500]", …). They pass through
# untouched and the UI's LRC parser drops them — treating them as blank keeps
# stored transforms line-aligned with what the player renders.
_META_LINE_RE = re.compile(r"^\[[a-zA-Z]+:.*\]$")

# Below this fraction of non-Latin letters a text counts as "already
# romanized" — no transliteration is requested or stored.
_LATIN_THRESHOLD = 0.15

# Sidecar suffix for the romanized lyrics (de-facto karaoke convention).
XLIT_SIDECAR = ".romaji.lrc"


def ai_ready(cfg):
    """True when Settings → AI has everything this script needs."""
    return bool(
        str(cfg.get("ai_base_url") or "").strip()
        and str(cfg.get("ai_model") or "").strip()
    )


def non_latin_ratio(text):
    """Fraction of alphabetic characters that are NOT Latin script.
    Whitespace, digits and punctuation are ignored — they carry no script."""
    letters = total = 0
    for ch in str(text or ""):
        if not ch.isalpha():
            continue
        total += 1
        try:
            if "LATIN" not in unicodedata.name(ch, ""):
                letters += 1
        except Exception:
            letters += 1
    return letters / total if total else 0.0


# Native script of the common non-Latin language codes (everything else is
# read in Latin script). Used to decide whether romanization is meaningful
# for the reader: a ja reader doesn't need Japanese lyrics romanized, an en
# reader does — regardless of what language the song is in.
_SCRIPT_BY_LANG = {
    "ja": "japanese", "zh": "han", "ko": "hangul",
    "ru": "cyrillic", "uk": "cyrillic", "bg": "cyrillic", "sr": "cyrillic",
    "mk": "cyrillic", "be": "cyrillic",
    "ar": "arabic", "fa": "arabic", "ur": "arabic",
    "he": "hebrew", "hi": "devanagari", "th": "thai", "el": "greek",
    "ka": "georgian", "hy": "armenian",
}


def lang_script(lang):
    """Script family of a language code (default: latin)."""
    return _SCRIPT_BY_LANG.get(str(lang or "").strip().lower().split("-")[0], "latin")


# The scripts whose letters belong to ONE language, so the text itself states
# it (kana is Japanese, hangul is Korean). Cyrillic and Han are deliberately
# absent: they carry several languages each, which is exactly the case a
# DECLARED language settles and a script cannot. Arabic is HERE, though it
# carries Persian and Urdu too, because it is what the tag suffix has always
# read (`TRANSLITERATION-AR-LATN`) and a reader of Arabic-script lyrics is
# served by the same answer either way.
_SCRIPT_LANG = {
    "japanese": "ja", "hangul": "ko", "greek": "el", "hebrew": "he",
    "arabic": "ar", "thai": "th", "devanagari": "hi", "georgian": "ka",
    "armenian": "hy",
}

# MusicBrainz states a release's language as an ISO 639-3 code and its script as
# an ISO 15924 one (``{"language": "jpn", "script": "Jpan"}``), and the app's own
# tables are keyed by the 639-1 codes a LANGUAGE tag holds — so the two meet
# here, in ONE map. It only exists at all because a Latin-script language is
# the one thing a text cannot state about itself: English and German look
# identical to a script test.
_MB_LANG = {
    "jpn": "ja", "eng": "en", "deu": "de", "ger": "de", "fra": "fr", "fre": "fr",
    "spa": "es", "ita": "it", "por": "pt", "nld": "nl", "dut": "nl",
    "kor": "ko", "zho": "zh", "chi": "zh", "rus": "ru", "ukr": "uk",
    "ell": "el", "gre": "el", "heb": "he", "ara": "ar", "hin": "hi",
    "tha": "th", "kat": "ka", "geo": "ka", "hye": "hy", "arm": "hy",
    "fas": "fa", "per": "fa", "urd": "ur", "ben": "bn", "srp": "sr",
    "bul": "bg", "mkd": "mk", "bel": "be", "pol": "pl", "tur": "tr",
    "swe": "sv", "nor": "no", "dan": "da", "fin": "fi", "isl": "is",
    "ice": "is", "ces": "cs", "cze": "cs", "slk": "sk", "slo": "sk",
    "slv": "sl", "hrv": "hr", "hun": "hu", "ron": "ro", "rum": "ro",
    "lit": "lt", "lav": "lv", "est": "et", "vie": "vi", "ind": "id",
    "glg": "gl", "cat": "ca", "eus": "eu", "baq": "eu", "cym": "cy",
    "wel": "cy", "gle": "ga", "iri": "ga", "lat": "la",
}

# Codes that state NOTHING about the lyrics, however official they look:
# ``mul`` (several languages at once), ``und`` (undetermined), ``zxx`` (no
# linguistic content), the collective/uncoded ranges — and the app's own empty
# tag. MusicBrainz really answers ``mul`` (a compilation), and every one of
# these means "ask something else" rather than "this is the language".
_NO_LANGUAGE = ("", "mul", "und", "zxx", "mis", "qaa", "qbb", "qlg")


def normalize_lang(code):
    """One language code in the app's own shape — a bare lower-case 639-1 code,
    or "" when the code states nothing.

    Accepts what MusicBrainz gives (``jpn``), what a tag holds (``ja``,
    ``ja-JP``, ``jpn; ja``, ``Japanese``'s own code) and every spelling the app
    writes itself, so a declared language from any source can be compared to
    another.
    """
    raw = str(code or "").strip().lower()
    if not raw:
        return ""
    raw = re.split(r"[-,;/]", raw)[0].strip()
    if raw in _NO_LANGUAGE:
        return ""
    return _MB_LANG.get(raw, raw)


def declared_language(*codes):
    """The first source that actually STATES a language, normalized — the
    tracks' ``LANGUAGE`` tag (an import stamps MusicBrainz's own answer there,
    and a run of this script stores the model's), then anything else a caller
    has. "" when none of them says anything."""
    for code in codes:
        out = normalize_lang(code)
        if out:
            return out
    return ""


def detect_language(text, cfg=None, *declared):
    """The language one track's lyrics are in, or "" when nothing can say.

    The evidence, in the order it is trusted — and the order is about how much
    each source is about THE LYRICS themselves:

    1. a DECLARED language (the track's own ``LANGUAGE`` tag) whose script
       agrees with the text's: an import writes MusicBrainz's
       ``text-representation`` there, and an earlier run of this rule writes
       what a model answered. A declared language the text's own script
       CONTRADICTS is not evidence about this text and is passed over — the
       lyrics are what is being transformed, and a wrong tag must never
       romanize or skip the wrong thing;
    2. the text's own SCRIPT, for the scripts that belong to one language
       (kana → ja, hangul → ko, … — `_SCRIPT_LANG`);
    3. the function words of the Latin languages a reader realistically
       configures (`_latin_lang`), which is all the evidence a text with no tag
       has.

    "" is an ANSWER: script 17 takes it to the model per track and STORES the
    answer, so the question is paid once and grading reads what was stored
    (spec R167).
    """
    src = dominant_script(text)
    for code in declared:
        lang = declared_language(code)
        if lang and lang_script(lang) == src:
            return lang
    if src != "latin":
        return _SCRIPT_LANG.get(src, "")
    return _latin_lang(text)


def dominant_script(text):
    """The script family carrying most of the text's letters."""
    counts: dict = {}
    for ch in str(text or ""):
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        if "KATAKANA" in name or "HIRAGANA" in name:
            key = "japanese"
        elif "HANGUL" in name:
            key = "hangul"
        elif "CJK" in name or "IDEOGRAPH" in name:
            key = "han"
        elif "CYRILLIC" in name:
            key = "cyrillic"
        elif "ARABIC" in name:
            key = "arabic"
        elif "HEBREW" in name:
            key = "hebrew"
        elif "DEVANAGARI" in name:
            key = "devanagari"
        elif "THAI" in name:
            key = "thai"
        elif "GEORGIAN" in name:
            key = "georgian"
        elif "ARMENIAN" in name:
            key = "armenian"
        elif "GREEK" in name:
            key = "greek"
        elif "LATIN" in name:
            key = "latin"
        else:
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return "latin"
    # Kana decide it: a text with any of it is Japanese even when the kanji
    # outnumber the kana (they usually do). Without this, a ja reader is asked
    # for a romanization of their own lyrics and the tag reads plain LATN.
    if counts.get("japanese"):
        return "japanese"
    return max(counts, key=lambda k: counts[k])


# Function words of the languages a reader realistically configures. Script
# detection cannot separate the languages that share a script — English and
# German lyrics look identical to `dominant_script` — so the reader-language
# test needs the only evidence left without a model: the words every language
# repeats in nearly every line. Small and deliberately strict; an unrecognised
# text is treated as NOT foreign, so grading never demands a translation on a
# guess.
_LATIN_WORDS = {
    "en": frozenset("the and you that with not are was his her they this from "
                    "have one all my of to in is it".split()),
    "de": frozenset("der die das und ich nicht ein eine einen ist du wir mit "
                    "auf dich sich sind bin".split()),
    "fr": frozenset("le la les et je ne pas un une est vous nous avec dans "
                    "pour que qui".split()),
    "es": frozenset("el la los las y que no un una es tu yo con para por "
                    "como mas".split()),
    "it": frozenset("il lo la gli e che non un una sono con per nel come "
                    "questo".split()),
    "pt": frozenset("os as e que nao um uma com para por voce eu meu "
                    "não você".split()),
    "nl": frozenset("het een en ik niet van dat met voor zijn ben "
                    "je".split()),
}


def _latin_lang(text):
    """Best guess at the language of a Latin-script text, "" when undecided.

    A plain vote among `_LATIN_WORDS`; a tie (words shared by two languages,
    or none at all) is no answer rather than a coin flip."""
    tokens = re.findall(r"[^\W\d_]+", str(text or "").lower(), re.UNICODE)
    if not tokens:
        return ""
    counts = {lang: sum(1 for t in tokens if t in words)
              for lang, words in _LATIN_WORDS.items()}
    best = max(counts, key=lambda k: counts[k])
    if counts[best] < 1:
        return ""
    if sorted(counts.values(), reverse=True)[1] == counts[best]:
        return ""
    return best


def _body_lines(text):
    """The lyric lines a transform would actually cover: timestamp chrome and
    ``[ar:…]`` metadata stripped, blanks dropped. Fewer than two of them is
    not a lyric (see `xlit_needs`)."""
    out = []
    for line in str(text or "").splitlines():
        _prefix, body = _split_lrc_line(line)
        if body and not _META_LINE_RE.match(body):
            out.append(body)
    return out


def _in_reader_language(text, lang):
    """True when *text* is already in the reader's own language.

    Script decides it whenever either side is not Latin script: Japanese
    lyrics are the ja reader's own language, Cyrillic ones are not an
    English reader's. Two Latin-script languages are separated by
    `_latin_lang`, and an undecided text passes as the reader's own — a
    translation is only ever demanded on positive evidence."""
    src = dominant_script(text)
    native = lang_script(lang)
    if native != "latin" or src != "latin":
        return src == native
    return _latin_lang(text) in ("", str(lang or "").strip().lower())


def xlit_needs(text, cfg, declared=""):
    """What this track's own lyrics still need stored — the ONE rule.

    Script 17 asks this before writing anything and the grader asks it before
    judging what is stored, so the two can never disagree: a track is failed
    for a transform the script would not have written, or passed for one it
    would.

    * a text with fewer than two lyric lines (empty, instrumental, or the
      single placeholder line some providers hand back) needs neither;
    * TRANSLITERATION is script-based — Lyrics whose script is Latin cannot
      be romanized further, and lyrics already in the reader's own script (a
      ja reader with ja lyrics) gain nothing, so only a different non-Latin
      script is worth one;
    * TRANSLATION is language-based — one that is already in the reader's
      language (the first entry of ``lyrics_translation_langs``) needs none,
      and a needed translation is wanted for every configured language,
      which is what ``langs`` reports. The language is
      :func:`detect_language`'s answer, *declared* included: without it, two
      Latin-script languages were separated by a function-word vote that can
      only ever say "one of the seven the app knows" — a German track with no
      English stopwords in it was skipped as already-English, and a Latin
      transliteration of a Japanese song was translated as if the romanization
      were the lyrics' own language. With it, the release's own language (the
      import stamps MusicBrainz's) or the model's answer for this track decides
      for real, and the vote is what answers a text with nothing else.

    Returns ``{"transliteration": bool, "translation": bool, "langs": [...],
    "language": str}`` where ``langs`` is empty unless a translation is needed
    and ``language`` is what the decision was made from ("" when nothing could
    say).
    """
    out = {"transliteration": False, "translation": False, "langs": [],
           "language": ""}
    if len(_body_lines(text)) < 2:
        return out
    reader = primary_translation_lang(cfg)
    src = dominant_script(text)
    lang = detect_language(text, cfg, declared)
    out["language"] = lang
    if non_latin_ratio(text) >= _LATIN_THRESHOLD and src != "latin" \
            and lang_script(reader) != src:
        out["transliteration"] = True
    if lang:
        # A stated language decides it outright: the reader's own needs
        # nothing, anything else does.
        out["translation"] = lang != normalize_lang(reader)
    else:
        out["translation"] = not _in_reader_language(text, reader)
    if out["translation"]:
        out["langs"] = translation_langs(cfg)
    return out


def _same_essence(a, b):
    """True when two lyric texts are the same words ignoring case, spacing
    and punctuation — an AI "translation" that matches its source line for
    line (English → English) is a no-op, not a translation. Compared on the
    whole text; translations that only mirror some lines still differ enough
    to be worth keeping."""
    norm = lambda s: re.sub(r"[\W_]+", "", str(s or "").lower())
    return norm(a) == norm(b) and bool(norm(a))


def translation_langs(cfg):
    """Configured translation target languages, e.g. ["en"] or ["en","de"]."""
    raw = str(cfg.get("lyrics_translation_langs") or "").strip()
    langs = [s.strip().lower() for s in raw.replace(";", ",").split(",") if s.strip()]
    return langs or ["en"]


def primary_translation_lang(cfg):
    """First configured language — used for the TRANSLATION tag, the
    ``.<lang>.lrc`` sidecar name and the reader-script rule."""
    return translation_langs(cfg)[0]


def xlit_tag_suffix(cfg, text, af=None):
    """Language detail for the TRANSLITERATION tag suffix.

    Source language when knowable — the explicit LANGUAGE tag first (an import
    writes MusicBrainz's own answer there, this script writes what a model
    answered when nothing else could), then the dominant script for scripts
    unique to one language (kana → JA, hangul → KO, …) — always with the
    -LATN target-script subtag, so the tag reads like BCP-47:
    ``TRANSLITERATION-JA-LATN``. Plain ``LATN`` when the source language can't
    be pinned down (Cyrillic and Han each map to several languages)."""
    lang = ""
    if af is not None:
        lang = declared_language(af.get_tag("LANGUAGE"))
    if not lang:
        lang = _SCRIPT_LANG.get(dominant_script(text), "")
    return f"{lang}-latn" if lang else "latn"


def _split_lrc_line(line):
    """One lyrics line -> (timestamp prefix or '', body text without word
    tags). Body keeps its inner spacing; only the timing chrome is removed
    because transformed text can never carry the original word timings."""
    m = _LINE_TS_RE.match(line)
    prefix = m.group(1) if m else ""
    body = line[m.end():] if m else line
    body = _WORD_TAG_RE.sub("", body).rstrip()
    return prefix, body.strip()


def _merge_lines(original, bodies_by_index, new_bodies):
    """Rebuild the lyrics text: every non-blank body is replaced by its
    transform (same position), blank lines and timestamp prefixes pass
    through untouched. This keeps stored transforms line-aligned with the
    original lyrics so the UI can render them without re-alignment."""
    out = []
    for i, line in enumerate(original):
        if i in bodies_by_index:
            prefix, _body = _split_lrc_line(line)
            out.append(f"{prefix} {new_bodies[bodies_by_index[i]]}".strip())
        else:
            out.append(line.rstrip())
    return "\n".join(out)


def _transform_unique(config, bodies, mode, lang=""):
    """Transform the deduplicated non-blank bodies of one track.
    Returns {original body: transformed body} (missing entries stay
    untransformed — the caller keeps the original text there)."""
    if not bodies:
        return {}
    from server import ai  # lazy: keeps the plain CLI importable
    unique = list(dict.fromkeys(bodies))
    got = ai.transform_lines(config, unique, mode, lang)
    return dict(zip(unique, got))


def _apply(config, text, mode, lang=""):
    """Transform one full lyrics text; returns (new_text, changed) where
    changed is False when every line came back identical to the input
    (e.g. the AI echoing a Latin-script original)."""
    lines = str(text or "").splitlines()
    bodies, index_map = [], {}
    for i, line in enumerate(lines):
        _prefix, body = _split_lrc_line(line)
        if body and not _META_LINE_RE.match(body):
            index_map[i] = len(bodies)
            bodies.append(body)
    if not bodies:
        return text, False
    mapping = _transform_unique(config, bodies, mode, lang)
    new_bodies = [mapping.get(b, b) for b in bodies]
    new_text = _merge_lines(lines, index_map, new_bodies)
    # Word/syllable-level sync for the transformed lyrics: the original is
    # synced, so the romanization / translation must be too, at the
    # configured level (karaoke sweeps the transformed line; CJK sweeps
    # per character, and a kana character is one syllable).
    if config.get("lrc_enhanced_word_sync", True) and \
            any(_LINE_TS_RE.match(l) for l in lines):
        new_text = elrc_word_sync(
            new_text, level=str(config.get("lrc_sync_level") or "LINE").lower())
    return new_text, new_text != text


def _has_translation(tr_val, path, lang, sidecars):
    """Already processed? Embedded tag OR any accepted sidecar counts.
    *tr_val* comes from an exact per-language tag read — another language's
    translation must not mark this one as done."""
    if str(tr_val or "").strip():
        return True
    if sidecars:
        if os.path.isfile(os.path.splitext(path)[0] + f".{lang}.lrc"):
            return True
    return False


def _stored_transform_tags(af, kind):
    """The file's own keys for the stored *kind* transforms — the bare legacy
    name and every language-suffixed one — spelled the way this container
    stores them (vorbis comments verbatim, ``TXXX:…`` on ID3, freeform atoms
    in MP4), so they can be handed straight back to ``delete_tag``."""
    out = []
    for key in (af.all_tags() or {}):
        name = str(key).upper().rsplit(":", 1)[-1]
        if name == kind or name.startswith(kind + "-"):
            out.append(str(key))
    return out


def _xlit_sidecars(path, kind):
    """The sidecar files that carry the *kind* transform of one track.

    TRANSLITERATION has exactly one (``.romaji.lrc``); a translation has one
    ``<stem>.<lang>.lrc`` per language — the configured ones plus whatever an
    older configuration left behind, because a stale translation is exactly
    what the not-needed branch has to clear. ``<stem>.lrc`` is the lyrics
    sidecar itself and is never in this list."""
    base = os.path.splitext(path)[0]
    if kind == "TRANSLITERATION":
        return [base + XLIT_SIDECAR]
    out = []
    stem = os.path.basename(base).lower()
    try:
        entries = os.listdir(os.path.dirname(base) or ".")
    except OSError:
        return out
    for entry in entries:
        low = entry.lower()
        if low.startswith(stem + ".") and low.endswith(".lrc") \
                and low != stem + ".lrc" and not low.endswith(XLIT_SIDECAR):
            out.append(os.path.join(os.path.dirname(base), entry))
    return out


def _drop_stored_transforms(af, path, kind):
    """Remove every stored *kind* transform from the tags and the sidecars.

    Called when `xlit_needs` says the track's lyrics need none: the writer
    and the grader have to agree in BOTH directions, and a transform the rule
    no longer asks for would otherwise fail XLIT_UNNEEDED forever — the
    script only ever added, so nothing else could clear it. Returns True when
    something was actually removed.
    """
    removed = False
    for name in _stored_transform_tags(af, kind):
        if af.delete_tag(name):
            removed = True
    for sidecar in _xlit_sidecars(path, kind):
        try:
            os.remove(sidecar)
            removed = True
        except OSError:
            pass
    return removed


def run_lyrics_xlit(config):
    """Script 17 entry point: write TRANSLITERATION / TRANSLATION tags and
    .romaji.lrc / .<lang>.lrc sidecars for the library (or run targets)."""
    folder = config.get("music_folder") or ""
    stats = new_stats()
    stats["transliterated"] = 0
    stats["translated"] = 0
    stats["latin_skipped"] = 0
    stats["identity_skipped"] = 0
    # Transforms DELETED because the track's lyrics no longer need them (see
    # _drop_stored_transforms) — the reverse of the writes, and the only way
    # a stale tag from an older, laxer rule can be cleared.
    stats["stale_removed"] = 0
    # Tracks whose lyrics' language was ASKED of the model and stored in the
    # LANGUAGE tag (nothing else could state it) — the one thing this script
    # writes that is not a transform, and what keeps the question from being
    # asked again (spec R167).
    stats["language_set"] = 0

    print_header("Lyrics Transliterate & Translate (AI)")
    force = bool(config.get("force_xlit", False))
    sidecars = (bool(config.get("lyrics_xlit_sidecars", True))
                and str(config.get("lyrics_format", "EMBEDDED")).upper() in ("LRC", "BOTH"))
    # With EMBEDDED lyrics the transforms are stored in the tags only —
    # sidecar files appear only for LRC/BOTH; with LRC-only the tags are
    # skipped so the two storage places never disagree.
    embed_tags = str(config.get("lyrics_format", "EMBEDDED")).upper() in ("EMBEDDED", "BOTH")
    do_xlit = bool(config.get("lyrics_xlit_enabled", True))
    do_trans = bool(config.get("lyrics_translate_enabled", True))
    langs = translation_langs(config)

    if not do_xlit and not do_trans:
        log("Both transliteration and translation are disabled in Settings.")
        return stats
    if not ai_ready(config):
        log(c("AI is not configured — set Base URL + model in Settings → AI "
              "(presets available, e.g. Google Gemini).", Color.YELLOW))
        return stats

    log("write mode: "
        + ("tags TRANSLITERATION-<lang>/TRANSLATION-<lang>"
           if embed_tags else "tags skipped (LRC lyrics format)")
        + (f" + sidecars (.romaji.lrc, {', '.join('.' + l + '.lrc' for l in langs)})"
           if sidecars else " (no sidecars)")
        + ("  (forced: re-transform existing)" if force else ""))

    if config.get("targets") is not None:
        files = sorted(_collect_targets(config["targets"], AUDIO_EXTS))
    else:
        if not os.path.isdir(folder):
            log(c(f"ERROR: folder does not exist: {folder}", Color.RED))
            return stats
        files = []
        for album_dir in _find_albums(folder):
            files.extend(sorted(
                os.path.join(album_dir, f)
                for f in os.listdir(album_dir) if is_audio_file(f)
            ))

    if not files:
        log("No audio files found.")
        return stats

    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(total=len(files), desc="Lyrics xlit/translate")

    def _one(path):
        """One track's transforms, off the runner thread.

        Returns the row to count instead of touching the run's stats: the row
        dict is this call's own, so N of these can run at once without a lock
        around the counters. It has to be a pool — this script is the one
        part of the chain that is pure waiting (one HTTP request per 40-line
        chunk, per language, per track), and a 31-track album paid one request
        after another is minutes of a stalled chain for work that overlaps
        freely.
        """
        row = {"transliterated": 0, "translated": 0, "latin_skipped": 0,
               "identity_skipped": 0, "stale_removed": 0, "language_set": 0,
               "changed": False, "error": ""}
        try:
            af = AudioFile(path)
            if af.audio is None:
                raise RuntimeError(af.error or "unreadable")

            instrumental = str(af.get_tag("INSTRUMENTAL") or "").strip() == "1"
            # Original lyrics: embedded first, .lrc sidecar as fallback —
            # the same resolution order the player uses.
            text = (af.get_lyrics() or "").strip()
            if not text:
                lrc_path = _lrc_for(path)
                if os.path.isfile(lrc_path):
                    try:
                        with open(lrc_path, "r", encoding="utf-8",
                                  errors="replace") as fh:
                            text = fh.read().strip()
                    except OSError:
                        text = ""
            if instrumental or not text:
                row["skipped"] = True
                return row

            changed = False
            # WHAT LANGUAGE THESE LYRICS ARE IN (spec R167), from the evidence
            # the app already has: the track's own LANGUAGE tag — an import
            # stamps MusicBrainz's release language there — and the text's own
            # script and function words. When neither can say (two
            # Latin-script languages with no shared stopwords is the case
            # that matters), the model is asked ONE question about this track,
            # and its answer is WRITTEN into the tag: grading and every later
            # run then read the same stored answer instead of asking again, and
            # the grader never asks a model anything at all. Filled only —
            # a tag another source stated (the import's MusicBrainz language,
            # the user's own edit) is never overwritten.
            tag_lang = str(af.get_tag("LANGUAGE") or "")
            lang = detect_language(text, config, tag_lang)
            if not lang:
                try:
                    from server import ai as ai_mod

                    lang = ai_mod.detect_language(config, text)
                except Exception:
                    lang = ""
                if lang and not normalize_lang(tag_lang):
                    try:
                        if af.set_tag("LANGUAGE", lang):
                            row["language_set"] = 1
                            changed = True
                    except Exception:
                        pass
            # What this track's own lyrics still need, from the ONE rule the
            # grader asks too (xlit_needs, with the language just resolved): a
            # transform the rule does not ask for is never written, so a re-run
            # leaves an already-correct track untouched and grading can never
            # disagree with what this loop decided.
            need = xlit_needs(text, config, declared=lang)

            # ---- transliteration ------------------------------------
            if do_xlit:
                existing = str(af.get_lyrics_transform("TRANSLITERATION") or "").strip()
                has_sidecar = sidecars and os.path.isfile(
                    os.path.splitext(path)[0] + XLIT_SIDECAR)
                if not need["transliteration"]:
                    # Already romanized, or already in the reader's own
                    # script (a ja reader doesn't need ja lyrics in
                    # romaji) — generating one would be a no-op. What is
                    # STORED is removed rather than merely skipped: this
                    # rule is what the grader judges by, so a transform
                    # written under an older, laxer rule would otherwise
                    # fail XLIT_UNNEEDED forever.
                    row["latin_skipped"] += 1
                    if _drop_stored_transforms(af, path, "TRANSLITERATION"):
                        row["stale_removed"] += 1
                        changed = True
                elif not force and (existing or has_sidecar):
                    pass  # already stored and still needed — keep it
                else:
                    xlit, ok = _apply(config, text, "transliterate")
                    if ok:
                        if embed_tags:
                            if af.set_tag(
                                    f"TRANSLITERATION-{xlit_tag_suffix(config, text, af)}".upper(),
                                    xlit):
                                # The bare legacy name is superseded — but
                                # only once the suffixed tag actually
                                # landed: deleting it after a refused
                                # write lost the transform outright.
                                if str(af.get_tag("TRANSLITERATION") or "").strip():
                                    af.delete_tag("TRANSLITERATION")
                        if sidecars:
                            _atomic_write_text(
                                os.path.splitext(path)[0] + XLIT_SIDECAR, xlit)
                        row["transliterated"] += 1
                        changed = True

            # ---- translation ----------------------------------------
            # Nothing to translate when the lyrics are already in the
            # reader's own language (need["langs"] is empty then) — the
            # model is not asked for the no-op translation _same_essence
            # below would only throw away. Stored translations are
            # removed instead: they are exactly what the grader rejects
            # as XLIT_UNNEEDED (see the transliteration pass).
            if do_trans:
                if not need["translation"] \
                        and _drop_stored_transforms(af, path, "TRANSLATION"):
                    row["stale_removed"] += 1
                    changed = True
                for lang in need["langs"]:
                    if not force and _has_translation(
                            af.get_lyrics_transform("TRANSLATION", lang, exact=True),
                            path, lang, sidecars):
                        continue
                    trans, ok = _apply(config, text, "translate", lang)
                    if ok and _same_essence(trans, text):
                        # The "translation" came back identical to the
                        # source (e.g. English → English): storing it
                        # would just duplicate every line in the player.
                        row["identity_skipped"] += 1
                        continue
                    if ok:
                        if embed_tags:
                            # one tag per configured language:
                            # TRANSLATION-EN, TRANSLATION-DE, …
                            if af.set_tag(f"TRANSLATION-{lang}".upper(), trans):
                                # Only delete the legacy bare name once
                                # the per-language tag is really on the
                                # file (see the transliteration pass).
                                if str(af.get_tag("TRANSLATION") or "").strip():
                                    af.delete_tag("TRANSLATION")
                        if sidecars:
                            _atomic_write_text(
                                os.path.splitext(path)[0] + f".{lang}.lrc", trans)
                        row["translated"] += 1
                        changed = True
            row["changed"] = changed
        except Exception as e:
            row["error"] = f"{os.path.basename(path)}: {e}"
        return row

    def _finish(row):
        """Apply one worker's row to the run's stats and the bar."""
        if row["error"]:
            stats["error_count"] += 1
            if len(stats["errors"]) < 25:
                stats["errors"].append(row["error"])
            _pbar_update(pbar, counts, "fail")
            return
        for key in ("transliterated", "translated", "latin_skipped",
                    "identity_skipped", "stale_removed", "language_set"):
            stats[key] += row[key]
        if row.get("skipped"):
            stats["skipped_count"] += 1
            _pbar_skip(pbar, counts)
            return
        stats["total_scanned"] += 1
        if row["changed"]:
            stats["modified_count"] += 1
            _pbar_update(pbar, counts, "ok")
        else:
            stats["unchanged_count"] += 1
            _pbar_skip(pbar, counts)

    # The chain serialises SCRIPTS; the requests inside one script have no
    # reason to be serial too. worker_limit bounds how many run at once, and
    # the AI layer's per-chunk disk cache is lock-protected, so two tracks
    # asking for the same transform still pay for it once.
    workers = worker_count(config, default=4, maximum=8, items=len(files))
    try:
        if len(files) == 1 or workers == 1:
            for path in files:
                _finish(_one(path))
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(_one, p): p for p in files}
                for fut in as_completed(futures):
                    _finish(fut.result())
    finally:
        try:
            pbar.close()
        except Exception:
            pass

    log(c(
        f"transliterated {stats['transliterated']}"
        f" · translated {stats['translated']}"
        f" · stale removed {stats['stale_removed']}"
        f" · language stored {stats['language_set']}"
        f" · latin-only skipped {stats['latin_skipped']}"
        f" · identical skipped {stats['identity_skipped']}"
        f" · unchanged {stats['unchanged_count']}"
        f" · errors {stats['error_count']}",
        Color.GREEN if stats["error_count"] == 0 else Color.YELLOW,
    ))
    return stats
