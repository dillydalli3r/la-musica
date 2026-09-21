#!/usr/bin/env python3
"""Lyrics profanity scan (mlo.advisory_words) — offline contract.

The scan is the last-resort ITUNESADVISORY signal: it runs when no provider
states a rating for a track, and both of its failures are expensive. A miss
leaves an explicit track looking clean; a false positive marks a clean track
explicit, and the classic spellings of that mistake are the reasons the
lexicon is curated at all — "ass" inside "class", "cunt" inside "Scunthorpe",
the fist name "Dick".

Pinned here, with no provider and no network:
  * every covered language has a line that hits and a clean line that does not;
  * the spelling traps stay silent, and the words deliberately kept out of the
    lexicon stay out;
  * the dodges lyrics sites use (asterisks, spaced letters, leet, repeated
    letters) are seen through, and none of them invents a hit;
  * LRC scaffolding — timestamps, section headers, metadata and credit lines —
    is what scan_lyrics strips, so a "[ti:Shitty Song]" is never the reason a
    track is called explicit;
  * scan is case-insensitive, deduplicated, stable, and reports what it found
    in first-appearance order.

Run: python tools/test_advisory_words.py
Exit 0 = pass.
"""
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The module is stdlib-only and the scanner is 3.10+ (its annotations and the
# builtin generics it subscripts); the app's interpreter is 3.13, so this guard
# only ever fires on a stray older one.
if sys.version_info < (3, 10):
    print("SKIP: needs Python 3.10+")
    raise SystemExit(2)

from mlo import advisory_words as aw  # noqa: E402  (after the version guard)

# Every language the lexicon must carry. The scan matches the union, so a
# missing tag is a whole language of lyrics going unread.
REQUIRED = ["en", "es", "fr", "de", "pt", "pt-BR", "it", "nl", "pl", "ru",
            "ru-Latn", "uk", "sv", "no", "da", "fi", "tr", "cs", "ro", "hu",
            "el", "ar", "he", "hi", "hi-Latn", "ja", "ko", "zh", "zh-Hant",
            "id", "vi", "th", "fil"]

# One line per tag, with the term the line must report (the tag list above is
# asserted equal to the lexicon's own tags, so a new language cannot land
# without a line here proving it works).
SAMPLES = {
    "en": ("Shut the fuck up and drive", "fuck"),
    "es": ("Ese cabrón me robó la guitarra", "cabrón"),
    "fr": ("Putain, c'est quoi ce bordel", "putain"),
    "de": ("Scheiße, das war laut", "scheiße"),
    "pt": ("Que se foda essa dor", "foda"),
    "pt-BR": ("Seu viado, sai daqui agora", "viado"),
    "it": ("Che cazzo dici stasera", "cazzo"),
    "nl": ("Kut, dat deed echt pijn", "kut"),
    "pl": ("Kurwa, co to jest znowu", "kurwa"),
    "ru": ("Иди нахуй отсюда", "иди нахуй"),
    "ru-Latn": ("Idi nahui otsyuda", "nahui"),
    "uk": ("Та йди ти нахуй звідси", "нахуй"),
    "sv": ("Jävla skit, vad trött jag är", "jävla"),
    "no": ("Faen, så dumt av meg", "faen"),
    "da": ("For helvede, mand", "forhelvede"),
    "fi": ("Vittu tämä on paska biisi", "vittu"),
    "tr": ("Siktir git buradan", "siktir"),
    "cs": ("Do prdele, to bolí", "prdele"),
    "ro": ("Ce pula mea faci acolo", "pula"),
    "hu": ("A kurva életbe, megint", "kurva"),
    "el": ("Μαλάκα, τι κάνεις εκεί", "μαλάκα"),
    "ar": ("يا ابن الشرموطة", "الشرموطة"),
    "he": ("יא זונה, לך מכאן", "זונה"),
    "hi": ("चूतिया, निकल यहाँ से", "चूतिया"),
    "hi-Latn": ("Chutiye, nikal yahan se", "chutiye"),
    "ja": ("このクソ野郎が", "クソ野郎"),
    "ko": ("씨발, 진짜 짜증나", "씨발"),
    "zh": ("你他妈的傻逼东西", "傻逼"),
    "zh-Hant": ("幹你娘, 走開啦", "幹你娘"),
    "id": ("Anjing, dasar bajingan", "anjing"),
    "vi": ("Địt mẹ mày, im đi", "địt"),
    "th": ("ไอ้เหี้ย อย่ามายุ่ง", "เหี้ย"),
    "fil": ("Putangina mo, gago ka", "putangina"),
}

# Mainstream lines that must produce nothing at all, in the languages whose
# everyday words sit closest to a term (Spanish "con", German "du", Romanian
# "după", Turkish "am", Vietnamese "tai" — the collisions the lexicon refuses).
SILENT = [
    "The grass is green and the class was fun",
    "I play bass guitar in a band called Cocoa",
    "Nothing but blue skies from now on",
    "Somewhere over the rainbow, way up high",
    "I said a prayer for the fallen and I meant it",
    "She sells sea shells by the sea shore",
    "Take the shortcut, cut the rope and pass the salt",
    "He got a new coat and a bucket of water",
    "One hour of silence in an honest heart",
    "I am what I am and that is all that I am",
    "I'm a fan of the ocean sound",
    "The loon and the lull of the harbour at night",
    "Du bist mein Herz und meine Seele",
    "La mer est calme ce matin et le ciel est bleu",
    "La playa esta tranquila y el sol brilla",
    "De lucht is blauw vandaag en de vogels zingen",
    "Nós vamos cantar juntos a noite inteira",
    "Мы идём по улице вдвоём и поём о любви",
    "După aceea am plecat acasă în liniște",
    "Seni seviyorum güzelim, bu gece bizim",
    "Kita bernyanyi bersama di pantai sore ini",
    "Hôm nay trời đẹp quá, mình đi chơi nhé",
    "Maganda ang umaga ngayon, kumanta tayo",
    "오늘 날씨가 좋네요, 같이 걸어요",
    "今日はいい天気ですね、散歩しましょう",
    "今天的天空很蓝，我们一起去散步吧",
    "วันนี้อากาศดีมาก เรามาเดินเล่นกัน",
    "Mera dil hai Hindustani, gaana suno",
    "אני אוהב את השיר הזה מאוד",
    "أنا أحب هذه الأغنية كثيرا",
    "Μου αρέσει αυτό το τραγούδι πολύ",
    "Kocham tę piosenkę i śpiewam ją głośno",
    "Szeretem ezt a dalt és hangosan éneklem",
    "Miluji tuhle píseň a zpívám ji nahlas",
    "Jag älskar den här sången och sjunger högt",
    "Jeg elsker denne sangen og synger høyt",
    "Rakastan tätä laulua ja laulan kovaa",
]

# The spelling traps. Each one CONTAINS a term as a substring, and every one
# of them is an ordinary word, a place or a person's name.
TRAPS = [
    "class", "grass", "bass guitar", "Dick Dale", "Scunthorpe", "assassin",
    "shiitake", "cocoa", "Cockburn Street", "Sussex", "Essex",
    "The assassin ate shiitake mushrooms in Scunthorpe with Dick Dale",
]

# Attack, then the one term it must report. The lexicon lists the evasive
# spellings too ("fuk", "fck"), so each of these is checked for the exact
# reading the scanner should reach — not for a bare non-empty answer.
EVASIONS = [
    ("f***ing amazing", ["fucking"]),
    ("ni**a please", ["nigga"]),
    ("sh*t happens", ["shit"]),
    ("what the f***", ["fuck"]),
    ("*sshole", ["asshole"]),
    ("b*tch", ["bitch"]),
    ("f.u.c.k", ["fuck"]),
    ("f-u-c-k", ["fuck"]),
    ("f_u_c_k", ["fuck"]),
    ("f u c k this", ["fuck"]),
    ("s h i t", ["shit"]),
    ("n1gga", ["nigga"]),
    ("sh!t", ["shit"]),
    ("a55hole", ["asshole"]),
    ("5hit", ["shit"]),
    ("fuuuuck", ["fuck"]),
    ("shiiiiiit", ["shit"]),
    ("baaaastard", ["bastard"]),
    ("F U C K", ["fuck"]),
    ("F*ck", ["fuck"]),
]

# LRC scaffolding: the metadata and section lines carry terms of their own, so
# a scan that did NOT strip them would answer here and a scan_lyrics that did
# would not.
LRC = ("[ar:The Fucks]\n"
       "[ti:Shitty Song]\n"
       "[00:12.34]la la la\n"
       "[00:15.00][Chorus]\n"
       "la la la\n"
       "作词 : The Assholes\n"
       "[00:20.00]and we sing along")

print("advisory words: checking the lexicon")
assert len(aw.WORDS_BY_LANG) >= 30, len(aw.WORDS_BY_LANG)
assert set(aw.WORDS_BY_LANG) == set(REQUIRED), (
    sorted(set(aw.WORDS_BY_LANG) ^ set(REQUIRED)))
assert len(aw.WORDS) >= 400, len(aw.WORDS)
assert aw.WORDS == frozenset(
    t for terms in aw.WORDS_BY_LANG.values() for t in terms), "union mismatch"
for lang, terms in aw.WORDS_BY_LANG.items():
    assert isinstance(terms, frozenset) and terms, lang
# Every term is a plain word (or a phrase of them): letters only, no
# punctuation and no digits, because a term is also the regex body that matches
# it. Combining marks count — Thai and Devanagari vowel signs are not letters
# but belong to the word.
for term in aw.WORDS:
    assert term == term.lower(), term
    assert term == term.strip() and "  " not in term, repr(term)
    assert len(term) >= 2, term
    assert unicodedata.is_normalized("NFC", term), repr(term)
    assert unicodedata.is_normalized("NFKC", term), repr(term)
    assert all(ch == " " or unicodedata.category(ch)[0] in ("L", "M")
               for ch in term), repr(term)
    # No term is spelled with a run of three identical letters: scan collapses
    # those in the TEXT (that is what reads "fuuuuck"), so an entry that
    # carried one could never match.
    assert not re.search(r"(\w)\1{2,}", term), repr(term)
# The words kept out because they are ordinary words or people's names in some
# covered language. Each has been a real false positive somewhere.
for absent in ("dick", "willy", "randy", "roger", "hell", "damn", "crap",
               "bloody", "idiot", "moron", "con", "fan", "hora", "am", "cu",
               "cur", "cut", "du", "satan", "got", "bok", "tai", "skid",
               "vögel", "eikel", "tanga", "bobo", "leche", "hayop", "porco",
               "fica", "curva", "pina", "amina", "svin"):
    assert absent not in aw.WORDS, absent

print(f"advisory words: {len(SAMPLES)} language lines")
assert set(SAMPLES) == set(aw.WORDS_BY_LANG), "a language has no sample line"
for lang, (line, expected) in SAMPLES.items():
    hits = aw.scan(line)
    assert expected in hits, (lang, line, hits)

print(f"advisory words: {len(SILENT) + len(TRAPS)} clean lines")
for line in SILENT + TRAPS:
    hits = aw.scan(line)
    assert hits == [], (line, hits)
# The traps again, stripped to the bare word: the guard is about the token, so
# "assassin" and "Dick Dale" must fail for the same reason "ass" alone passes.
assert aw.scan("ass") == ["ass"]
assert aw.scan("assassin") == [] and aw.scan("grass") == []
assert "dickhead" in aw.WORDS and aw.scan("dickhead") == ["dickhead"]

print(f"advisory words: {len(EVASIONS)} evasions")
for line, expected in EVASIONS:
    hits = aw.scan(line)
    assert hits == expected, (line, hits, expected)
# A censored word never turns an innocent neighbour into a hit, and the
# separator tolerance never glues two ordinary words into one.
assert aw.scan("f*** off") == ["fuck"], aw.scan("f*** off")
assert aw.scan("as soon as possible") == []
assert aw.scan("a class act") == []
assert aw.scan("pass the bass to the class") == []

print("advisory words: LRC scaffolding")
assert aw.scan(LRC) == ["fucks", "shitty", "assholes"], aw.scan(LRC)
assert aw.scan_lyrics(LRC) == [], aw.scan_lyrics(LRC)
assert aw.scan_lyrics(LRC + "\n[00:31.00]what the fuck") == ["fuck"]
assert aw.scan_lyrics("[00:00.00][00:45.53]shit") == ["shit"]
assert aw.scan_lyrics("[Chorus: The Fucks]\nla la la") == []
assert aw.scan_lyrics("[ti:Shitty Song]\n[ar:The Assholes]") == []
assert aw.scan_lyrics("Lyrics: The Fucks\nla la la") == []
# An empty lyric file is not a reason to call anything explicit.
assert aw.scan_lyrics("") == [] and aw.scan("") == []

print("advisory words: case, order and stability")
assert aw.scan("FUCK") == ["fuck"]
assert aw.scan("FuCkInG") == ["fucking"]
assert aw.scan("Scheiße") == ["scheiße"] and aw.scan("SCHEISSE") == ["scheisse"]
assert aw.scan("fuck shit") == ["fuck", "shit"]        # first appearance wins
assert aw.scan("shit, then fuck") == ["shit", "fuck"]
assert aw.scan("fuck fuck FUCK f***ing") == ["fuck", "fucking"]
assert aw.scan("fuck this shit, fuck it, shit again") == ["fuck", "shit"]
# Same text, same answer — the cache and the tie-breaks are both deterministic
# — and the answer is a list a caller can join.
assert aw.scan("shit fuck") == aw.scan("shit fuck") == ["shit", "fuck"]
assert ", ".join(aw.scan("fuck shit")) == "fuck, shit"
# The cache is keyed on the text: a second, different line is not answered
# from the first one's result.
assert aw.scan("what a lovely day") == []
assert aw.scan("you fucking idiot") == ["fucking"]

print("advisory words: the term reported is the whole word")
# Which term a spelling reports is scored, not guesswork: the reading that
# stretches over the fewest separators wins, and the longest one after that, so
# a compound cannot be read out of two words and a word drawn out or censored
# is still the whole word.
assert aw.scan("fuck up") == ["fuck"], aw.scan("fuck up")
assert aw.scan("piss") == ["piss"], aw.scan("piss")
assert aw.scan("asses") == ["asses"], aw.scan("asses")
assert aw.scan("chutiya") == ["chutiya"], aw.scan("chutiya")
assert aw.scan("scumbag gandu") == ["gandu"], aw.scan("scumbag gandu")
# A phrase the lexicon lists is reported as itself when it starts the place.
assert aw.scan("hijo de puta") == ["hijo de puta"]
assert aw.scan("vai tomar no cu") == ["vai tomar no cu"]
# Two words that only look like a compound when glued are still the phrase the
# lexicon lists — there is nothing shorter to report.
assert aw.scan("mother fucker") == ["motherfucker"]

print("advisory words: normalize, extra, min_len")
assert aw.normalize("  Hello\u00a0 WORLD  ") == "hello world"
assert aw.normalize("ＦＵＣＫ") == "fuck"
assert aw.normalize("") == ""
assert aw.scan("banana banana") == []
assert aw.scan("banana", extra=["banana"]) == ["banana"]
assert aw.scan("b a n a n a", extra=["banana"]) == ["banana"]
assert aw.scan("BANANA!", extra=["Banana"]) == ["banana"]
assert aw.scan("ass", min_len=2) == ["ass"]
assert aw.scan("ass", min_len=4) == []
assert aw.scan("asses", min_len=4) == ["asses"]

print(f"advisory words: {len(aw.WORDS_BY_LANG)} languages, {len(aw.WORDS)} "
      f"entries, {len(SAMPLES)} language lines, {len(SILENT)} clean lines, "
      f"{len(TRAPS)} traps, {len(EVASIONS)} evasions — all assertions passed")
