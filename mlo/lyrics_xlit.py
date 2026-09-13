"""Script 15 — Lyrics Transliterate & Translate (AI).

Turns the fullscreen player's ephemeral AI transforms into durable library
data. For every track with lyrics it:

* transliterates non-Latin lyrics into Latin script (romanization). Lyrics
  that are already Latin script are treated as their own transliteration —
  nothing is written for them (romanizing Spanish would be a no-op);
* translates the lyrics into every language configured in
  ``lyrics_translation_langs`` (comma separated, e.g. "en,de");

and stores the results in two places:

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
re-alignment.

The AI itself comes from the shared OpenAI-compatible client in
``server/ai`` (imported lazily so the plain CLI still works); results are
disk-cached there per (mode, language, content), so re-runs only pay for
new or changed lyrics.
"""
import os
import re
import unicodedata

from .audio import AudioFile
from .lyrics import _atomic_write_text, _lrc_for, elrc_word_sync
from .paths import AUDIO_EXTS
from .stats import (
    is_audio_file, new_stats, _collect_targets, _find_albums,
    _make_pbar, _pbar_skip, _pbar_update,
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
    """True when Settings → AI has everything script 15 needs."""
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
    return max(counts, key=lambda k: counts[k])


def needs_transliteration(text, cfg):
    """True when romanized lyrics are worth generating for this reader.

    The rule is script-based, driven by the configured locale (the primary
    translation target language): lyrics whose script is not Latin AND
    differs from the locale's own script need romanization. Japanese lyrics
    for a ja reader add nothing (their script matches); lyrics already in
    Latin script can't be romanized further."""
    if non_latin_ratio(text) < _LATIN_THRESHOLD:
        return False
    src = dominant_script(text)
    if src == "latin":
        return False
    return lang_script(primary_translation_lang(cfg)) != src


def needs_translation(text, cfg):
    """True when the reader plausibly needs a translation: the lyrics'
    dominant script differs from the locale's script. (Same-script pairs
    like French → en can't be told apart from English offline — script 15
    still translates them via the AI's own identity filter, but grading
    never demands what can't be detected.)"""
    return dominant_script(text) != lang_script(primary_translation_lang(cfg))


_EN_STOPWORDS = frozenset(
    "the be to of and a in that have i it for not on with he as you do at this but his by from they we say her she "
    "or an will my one all would there their what so up out if about who get which go me when make can like time "
    "no just him know take people into year your good some could them see other than then now look only come its "
    "over think also back after use two how our work first well way even new want because any these give day most "
    "us are is was were been am dont isnt wasnt cant didnt im thats thats".split()
)


def looks_english(text, threshold=0.28):
    """Cheap stopword-based English detector — enough to refuse an
    English→English translation request before it reaches the AI."""
    words = re.findall(r"[A-Za-z']+", str(text or ""))
    if len(words) < 12:
        return False
    hits = sum(1 for w in words if w.lower() in _EN_STOPWORDS)
    return hits / len(words) >= threshold


def _same_essence(a, b):
    """True when two lyric texts are the same words ignoring case, spacing
    and punctuation — an AI "translation" that matches its source line for
    line (English → English) is a no-op, not a translation. Compared on the
    whole text; translations that only mirror some lines still differ enough
    to be worth keeping."""
    norm = lambda s: re.sub(r"[\W_]+", "", str(s or "").lower())
    return norm(a) == norm(b) and bool(norm(a))


def translation_langs(cfg):
    """Configured translation target languages, e.g. ["en"] or ["en","de"].
    Falls back to the fullscreen player's single ``ai_translate_lang``."""
    raw = str(cfg.get("lyrics_translation_langs") or "").strip()
    langs = [s.strip().lower() for s in raw.replace(";", ",").split(",") if s.strip()]
    if not langs:
        langs = [str(cfg.get("ai_translate_lang") or "en").strip().lower() or "en"]
    return langs


def primary_translation_lang(cfg):
    """First configured language — used for the TRANSLATION tag, the
    ``.<lang>.lrc`` sidecar name and the grading check."""
    return translation_langs(cfg)[0]


def xlit_tag_suffix(cfg, text, af=None):
    """Language detail for the TRANSLITERATION tag suffix.

    Source language when knowable — the explicit LANGUAGE tag first, then
    the dominant script for scripts unique to one language (kana → JA,
    hangul → KO, …) — always with the -LATN target-script subtag, so the
    tag reads like BCP-47: ``TRANSLITERATION-JA-LATN``. Plain ``LATN`` when
    the source language can't be pinned down (Cyrillic and Han each map to
    several languages)."""
    lang = ""
    if af is not None:
        lang = str(af.get_tag("LANGUAGE") or "").strip().lower()
        lang = re.split("[-,;]", lang)[0] if lang else ""
    if lang in ("", "und", "zxx"):
        script = dominant_script(text)
        lang = {
            "japanese": "ja", "hangul": "ko", "greek": "el", "hebrew": "he",
            "arabic": "ar", "thai": "th", "devanagari": "hi",
            "georgian": "ka", "armenian": "hy",
        }.get(script, "")
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
    untranslated — the caller keeps the original text there)."""
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
    """Already processed? Embedded tag OR any accepted sidecar counts."""
    if str(tr_val or "").strip():
        return True
    if sidecars:
        if os.path.isfile(os.path.splitext(path)[0] + f".{lang}.lrc"):
            return True
    return False


def run_lyrics_xlit(config):
    """Script 15 entry point: write TRANSLITERATION / TRANSLATION tags and
    .romaji.lrc / .<lang>.lrc sidecars for the library (or run targets)."""
    folder = config.get("music_folder") or ""
    stats = new_stats()
    stats["transliterated"] = 0
    stats["translated"] = 0
    stats["latin_skipped"] = 0

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

    fmt = str(config.get("lyrics_format", "EMBEDDED")).upper()
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
    try:
        for path in files:
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
                    stats["skipped_count"] += 1
                    _pbar_skip(pbar, counts)
                    continue

                changed = False

                # ---- transliteration ------------------------------------
                if do_xlit:
                    existing = str(af.get_lyrics_transform("TRANSLITERATION") or "").strip()
                    has_sidecar = sidecars and os.path.isfile(
                        os.path.splitext(path)[0] + XLIT_SIDECAR)
                    if not force and (existing or has_sidecar):
                        pass  # already stored — keep it
                    elif not needs_transliteration(text, config):
                        # Already romanized, or already in the reader's own
                        # script (a ja reader doesn't need ja lyrics in
                        # romaji) — generating it would be a no-op.
                        stats["latin_skipped"] += 1
                    else:
                        xlit, ok = _apply(config, text, "transliterate")
                        if ok:
                            if embed_tags:
                                af.set_tag(
                                    f"TRANSLITERATION-{xlit_tag_suffix(config, text, af)}".upper(),
                                    xlit)
                                # the bare legacy name is superseded
                                if str(af.get_tag("TRANSLITERATION") or "").strip():
                                    af.delete_tag("TRANSLITERATION")
                            if sidecars:
                                _atomic_write_text(
                                    os.path.splitext(path)[0] + XLIT_SIDECAR, xlit)
                            stats["transliterated"] += 1
                            changed = True

                # ---- translation ----------------------------------------
                if do_trans:
                    for lang in langs:
                        if not force and _has_translation(
                                af.get_lyrics_transform("TRANSLATION", lang),
                                path, lang, sidecars):
                            continue
                        trans, ok = _apply(config, text, "translate", lang)
                        if ok and _same_essence(trans, text):
                            # The "translation" came back identical to the
                            # source (e.g. English → English): storing it
                            # would just duplicate every line in the player.
                            stats["translation_identity_skipped"] = (
                                stats.get("translation_identity_skipped", 0) + 1)
                            continue
                        if ok:
                            if embed_tags:
                                # one tag per configured language:
                                # TRANSLATION-EN, TRANSLATION-DE, …
                                af.set_tag(f"TRANSLATION-{lang}".upper(), trans)
                                if str(af.get_tag("TRANSLATION") or "").strip():
                                    af.delete_tag("TRANSLATION")
                            if sidecars:
                                _atomic_write_text(
                                    os.path.splitext(path)[0] + f".{lang}.lrc", trans)
                            stats["translated"] += 1
                            changed = True

                stats["total_scanned"] += 1
                if changed:
                    stats["modified_count"] += 1
                    _pbar_update(pbar, counts, "ok")
                else:
                    stats["unchanged_count"] += 1
                    _pbar_skip(pbar, counts)
            except Exception as e:
                stats["error_count"] += 1
                if len(stats["errors"]) < 25:
                    stats["errors"].append(f"{os.path.basename(path)}: {e}")
                _pbar_update(pbar, counts, "fail")
    finally:
        try:
            pbar.close()
        except Exception:
            pass

    log(c(
        f"transliterated {stats['transliterated']}"
        f" · translated {stats['translated']}"
        f" · latin-only skipped {stats['latin_skipped']}"
        f" · unchanged {stats['unchanged_count']}"
        f" · errors {stats['error_count']}",
        Color.GREEN if stats["error_count"] == 0 else Color.YELLOW,
    ))
    return stats
