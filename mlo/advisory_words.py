"""Lyrics profanity scan: the last-resort ITUNESADVISORY (explicit) signal.

WHY this module exists
----------------------
``integrations.resolve_advisory_route`` asks Deezer, Spotify and Apple for a
track's advisory and ``server.imports.fetch_advisories`` writes what they
state. When every one of them answers "no data" the track keeps no
ITUNESADVISORY at all — and a release whose lyrics are explicit then ships
looking clean. The lyrics the app has already fetched close that gap: this
module reports the profanity they contain, which is a reason to write 1.

The scan answers one question — *which terms from the lexicon occur in this
text?* — and answers it conservatively:

* **Whole tokens only.** ``ass`` never fires inside ``class``, ``grass`` or
  ``bass``. The terms that are also ordinary words or personal names in some
  covered language (``dick`` is a given name, ``con`` is Spanish for "with",
  ``fan`` is English for an admirer, ``am`` is English "am") are deliberately
  absent from the lexicon: a language-agnostic token scanner cannot tell them
  apart from the profanity, and a false "explicit" is worse than a miss.
* **Obfuscation is seen through**, because lyrics sites censor: ``f***ing``,
  ``f.u.c.k``, ``f u c k``, ``n1gga``, ``sh!t``, ``fuuuuck``.
* **Scripts written without spaces** (Japanese, Chinese, Thai — and Korean,
  which glues its particles onto the stem) have no token boundary to rely on,
  so their terms are matched as substrings; everywhere else a term must stand
  alone on its own. That is also why the single-character CJK terms that live
  inside ordinary compounds (``操`` in ``操场``, ``逼`` in ``逼真``) are left
  out in favour of the phrases that are unambiguous.
* **A lyric file is not a text file.** ``scan_lyrics`` drops the LRC
  scaffolding first, so the timestamps, the section headers and the provider's
  credit lines can never be the reason a track is called explicit — a
  ``[ti:Shitty Song]`` is a title, not a lyric.

The lexicon is data: plain lowercase frozensets, one per language tag, whose
union is published as ``WORDS``. The ONE pattern that matches all of them is
compiled from that union on the first scan: a ~1300-branch alternation costs
real time to compile, and a process that never scores a lyric should not pay
it at import.

Deliberately absent, because each one is a false positive waiting to happen —
they are ordinary words, animals or people's names in some covered language:
``dick``/``willy``/``randy``/``roger`` (given names), ``hell``/``damn``/
``crap``/``bloody``/``idiot``/``moron`` (not the accepted explicit marker in
English), ``satan`` (a proper noun), ``fan``/``svin``/``vögel``/``eikel``/
``porco``/``hora``/``leche``/``hayop`` (an admirer, a pig, a bird, an acorn, a
pig, an hour, milk, an animal — each profane in one covered language and an
ordinary word of another), ``con`` (French, but Spanish "with"), ``am``
(Turkish, but English "am"), ``cu`` (Portuguese and Romanian, but Romanian
"with"), ``cur`` (Romanian, but English "cur"), ``cut``/``du``/``dit``
(Vietnamese without its diacritics, but ordinary English and French),
``got``/``bok``/``amina``/``pina`` (Turkish, Norwegian and Hungarian without
their diacritics, but English "got", "book" in the Nordic languages, and given
names), ``tai`` (Indonesian, but Vietnamese for "ear"), ``fica`` (Italian, but
Portuguese "stays").
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Iterable

# --------------------------------------------------------------------------- #
# The lexicon — one entry list per language tag
# --------------------------------------------------------------------------- #
# Ordered by how much of it a mainstream lyric could plausibly carry: the
# strong Anglo-Saxon set and its inflections first, then the Romance, Germanic
# and Slavic sets, then the slurs in their own scripts, then the languages
# whose profanity is mostly phrasal. Entries are single tokens unless the term
# is only profane as a phrase (``hijo de puta``), which is kept in
# ``_RAW_PHRASES`` below so that ``split()`` cannot tear it apart.
_RAW_WORDS: dict[str, str] = {
    # English. Inflections are spelled out: the scan matches whole tokens, so
    # "fucking" is only seen because it is listed here — "fuck" alone would be
    # stopped by the boundary of the longer word.
    "en": """
        fuck fucks fucked fucker fuckers fucking fuckin fuk fukin fukking fck
        fvck phuck phucking fuckface fuckhead fuckwit fuckwits fuckup fuckups
        fucktard fucktards clusterfuck mindfuck motherfuck motherfucker
        motherfuckers motherfucking motherfuckin shit shits shitty shittier
        shitting shithead shitheads shitstain shitstorm shitless horseshit
        dipshit dipshits bullshit bullshitting bullshitter ass asses asshole
        assholes asshat asswipe assclown dumbass dumbasses jackass jackasses
        arse arses arsehole arseholes bastard bastards bitch bitches bitching
        bitchy sonofabitch cunt cunts cunty cock cocks cocksucker
        cocksuckers prick pricks twat twats wank wanker wankers wanking tosser
        tossers tosspot bollocks bollocking piss pissed pissing pussy pussies
        dickhead dickheads whore whores whoring slut sluts slutty skank skanks
        skanky hooker hookers bimbo bimbos retard retards retarded goddamn
        goddammit goddamned shag shagging bugger buggered buggery spaz
        nigger niggers nigga niggas niggaz faggot faggots fag fags faggotry
        dyke tranny trannies kike kikes spic spics wetback wetbacks chink
        chinks gook gooks raghead ragheads towelhead coon coons beaner
        """,
    # Spanish (Castilian and Latin American; the slurs are the Mexican ones
    # that appear in lyrics).
    "es": """
        puta putas puto putos putita putón putona cabrón cabrones cabrona
        cabronazo pendejo pendejos pendeja pendejada pendejadas mierda
        mierdas mierdero joder jódete jodete jodido jodida jodidos coño coños
        carajo carajos verga vergas chingar chinga chingas chingada chingado
        chingados pinche pinches gilipollas marica maricas maricón maricones
        joto jotos pija pijas polla pollas culo culos culero culera huevón
        huevones huevona mamón mamones mamada mamadas pinga pingas cojones
        cojón hostia hostias zorra zorras perra perras
        """,
    "fr": """
        putain putains pute putes pétasse pétasses salope salopes salaud
        salauds salopard connard connards connasse connasses enculé enculés
        enculée enculer encule nique niquer niqué niquée baiser baise baisé
        baisée bite bites chatte chattes merde merdes merdique merdier foutre
        foutu foutue couille couilles bordel branler branlette branleur
        branleuse chier chiasse chieur chieuse emmerder emmerdeur emmerdeuse
        pédé pédés tapette tapettes gouine gouines ducon zob teub schneck
        bougnoule bougnoules
        """,
    "de": """
        fick ficken fickt ficker fickte gefickt scheiße scheisse scheiß
        scheiss scheissdreck scheißkerl scheisskerl arsch arschloch
        arschlöcher arschgeige wichser wichse wichsen wichste hurensohn hure
        huren nutte nutten schlampe schlampen fotze fotzen schwanz schwänze
        pimmel vögeln vögelte bumsen bumst miststück dreckskerl drecksau
        verpiss missgeburt schwuchtel schwuchteln kanake kanacke kanaken
        neger negerin
        """,
    # Portuguese — the shared set, then the Brazilian spellings it does not
    # carry (the union is what the scan uses, so nothing is lost by splitting).
    "pt": """
        porra porras caralho caralhos merda merdas puta putas puto putos
        foder fodase foda fodido fodida fodidos punheta punhetas cacete
        caceta buceta bucetas piroca pirocas corno cornos corna sacanagem
        sacana arrombado arrombada arrombados desgraçado desgraçada otário
        putaria putona
        """,
    "pt-BR": """
        viado viados veado veados bicha bichas boiola boiolas babaca otario
        fdp xoxota xoxotas cacete arrombado sacana punheta puta caralho
        """,
    "it": """
        cazzo cazzi cazzone cazzoni cazzata cazzate coglionata stronzo stronza
        stronzi stronze stronzo merda merde puttana puttane puttanate
        vaffanculo fanculo minchia minchioni coglione coglioni figa
        troia troie mignotta mignotte bastardo bastarda bastardi porcodio
        culo culi inculare inculato inculata scopare scopata scopate
        pompino pompini zoccola zoccole ricchione ricchioni frocio froci
        """,
    "nl": """
        kut kuthoer kutwijf kutje klote klootzak klootzakken lul lullen
        lulhannes hoer hoeren neuken neukt neukte geneukt kanker
        kankerlijer tering teringlijer tyfus pleuris godverdomme verdomme
        godver schijt schijten schijterd reet kont kontgat schoft schoften
        mierenneuker mierenneukers flikker flikkers
        """,
    "pl": """
        kurwa kurwy kurwo kurwią chuj chuja chuje chujowy chujnia huj huja
        pizda pizdy pizdo jebać jebac jebany jebana jebane jebie jebnięty
        pojebany pojeb pojebie pierdolić pierdolic pierdolony pierdol
        pierdoli skurwysyn skurwysyny skurwiel wypierdalaj wypierdzielaj
        spierdalaj spierdolić dupa dupy dupku dupek dupie cipa cipka cipy
        kutas kutasa suka suki sukinsyn cwel cwele gówno gowno gowniak
        zajebisty zajebista
        """,
    # Russian, Cyrillic and the transliterations a Latin-only lyric site
    # prints (both spellings are common in the wild, so both are listed).
    "ru": """
        хуй хуя хую хуе хуё хуи хуйня хуйню хуйло хуёвый хуевый охуенный
        охуенно охуел охуела охуеть охуительный нахуй нахуя похуй нихуя
        хули хуле хуёво пизда пизды пизде пиздой пиздец пиздёж пиздюк
        пиздатый распиздяй блядь бляди блядина блядство бля блять блядун
        ебать ебаться ебал ебала ебаный ёбаный ебёт ебет ебут ёб ебнутый
        заебать заебал заебали выебал сука суки сучка сукин мразь мрази
        мразота гандон гандоны долбоёб долбоеб мудак мудаки мудила мудило
        залупа дерьмо дерьма говно говна говнюк говнюки срать сру срака
        сраный ссать жопа жопы жопой манда шлюха шлюхи пидор пидоры
        пидорас пидорасы педик чмо ублюдок ублюдки падла падлюка хер херня
        обосрался обосрать обоссаный
        """,
    "ru-Latn": """
        hui huya huy huyu huynya huylo huyevy ohuel ohuenno ohuyet ohuenny
        nahui nahuy nahuja pohuy nihuya huli hule pizda pizdy pizdec pizdezh
        pizdyuk pizdaty raspizdyay blyad blyadi blyadina blyadstvo blya blyat
        blyadun ebat ebatsya ebal ebala ebany yobany ebet ebut yob ebnuty
        zaebal zaebali zaebat vyebal suka suki suchka sukin mraz mrazi gandon
        gandony dolboyob dolboeb mudak mudaki mudila mudilo zalupa dermo
        govno govna govnyuk srat sraka srany ssat zhopa zhopu
        shlyuha shlyuhi pidor pidory pidoras pedik chmo ubludok ubludki padla
        hernya poher
        """,
    "uk": """
        курва курви хуй хуя хую хуйло хуйня пізда пізди піздець пиздець
        піздюк блядь бляди блять блядина блядство єбати ебати їбати їбав
        їбало ебало сука суки сучка гівно гівнюк гівна дерьмо мразь підар
        підарас підари педик залупа нахуй похуй срака срати дупа шльондра
        шлюха ублюдок чмо падла херня
        """,
    "sv": """
        jävlar jävla jävel jävligt djävlar djävla djävul helvete helvetes kuk
        kuken kukar fitta fittan fittig skit skiten skitstövel skitstövlar
        skithög skitsnack bög bögar bögen knulla knull
        knullar knullet rövhål rövhålet arsle arsel
        """,
    "no": """
        faen faens helvete helvetes dritt drit dritten drittsekk drittsekker
        jævla jævel jævlig jævler kuk kuken kuker fitte fitta fitter hore
        horer rævhøl rævhull rasshøl møkk møkka
        """,
    "da": """
        lort lorten lortet lorte fandens fanden forhelvede helvede helvedes
        skiderik skiderikker pis pisse pisser kusse kussen pik pikken
        fisse fissen luder ludere røvhul røvhullet røv røven kælling
        kællinger
        """,
    "fi": """
        vittu vitun vittuun vittumainen vittuilla vittuilu perkele perkeleen
        perkelet saatana saatanan saatanasti jumalauta helvetti helvetin
        helvetissä kusipää kusipäät kusipäinen paska paskaa paskan paskainen
        paskiainen mulkku mulkut mulkun kyrpä kyrvän kyrvät huora huoran
        huorat runkkari runkkarit runkata pillu pillun pillut kulli kullin
        perse persereikä
        """,
    "tr": """
        siktir sik sikerim sikik sikim sikeyim sikiyim sikecem siktirgit
        orospu orospular amcık amcik aminakoyim amına anani ananı
        yarrak yarrağı göt götü götveren götlek piç piçler kahpe sürtük
        sürtuk ibne şerefsiz serefsiz yavşak yavsak pezevenk kaltak boktan
        """,
    "cs": """
        kurva kurvy kurvě kurvou píča píčo píčus čurák curak hovno hovna
        sračka sracka srát srat sere jebat jebe mrdat mrdka mrdky zmrd zmrdi
        kokot kokoti kokotina prdel prdele prdelka kunda kundu svine svině
        hajzl hajzle vyjebat vysrat posrat
        """,
    "ro": """
        pula pulii pizda pizdă pizde căcat cacat căcaturi fut futu fututi
        fute futut futută muie muist muista morții mortii dracului dracu
        curvă laba labele coaie boule bulangiu
        """,
    "hu": """
        kurva kurvák kurvát kurvának fasz fasza faszom faszod faszfej
        faszszopó faszkalap geci gecis gecik picsa szar szarok
        szart szarházi bazd bazdmeg baszd baszdmeg baszom baszni baszott buzi
        buzis köcsög kocsog segg seggfej seggbe anyád anyad lófasz szopd
        szopjál
        """,
    "el": """
        μαλάκας μαλάκα μαλάκες μαλακία μαλακίες μαλακισμένος γαμώ γαμω
        γαμημένος γαμημένη γαμημένο γαμήσι γαμάω γαμώτο πούτσα πουτσα
        πουτσες πούστη πουστης πουστιά πουτάνα πουτανα μουνί μουνι μουνιά
        αρχίδια αρχιδια σκατά σκατα καριόλης καριολης μπάσταρδος κερατάς
        """,
    # Arabic glues its article and its prepositions onto the noun, so the
    # everyday spellings of the strongest terms are listed as they are written.
    "ar": """
        كس كسي كسك كسمك زب زبي زبر شرموط شرموطة شراميط قحبة قحبه عرص عرصة
        منيك نيك ينيك نياك خرا خراء عاهرة عاهره طيز طياز زانية لوطي خول
        متناك الكس الزب الشرموطة الشراميط القحبة العرص الطيز الخول الخرا
        العاهرة الزانية النيك المنيك المتناك
        """,
    # Hebrew's article and prefixed prepositions, spelled as they are written.
    "he": """
        זין זינים לזיין זיון זיונים חרא זונה זונות שרמוטה שרמוטות כוסאמק
        תזדיין מזדיין פאק דפוק דפוקה מניאק מנייק הומו הומואים הזונה הזונות
        השרמוטה החרא הזין הכוסאמק ההומו
        """,
    "hi": """
        चूत चूतड़ चूतिया चूतिये चूतिए भोसड़ी भोसड़ीके भोसडीके भोसड़ीवाले
        मादरचोद मदरचोद बहनचोद गांड गांडू लौड़ा लौड़े लंड रंडी रंडियां
        हरामी हरामखोर भड़वा झाट चोद चोदू
        """,
    "hi-Latn": """
        chutiya chutiye chutiyon choot chut bhosdi bhosdike bhosdiwale bhosda
        madarchod maderchod madarchodd behanchod behenchod gaand gand gandu
        gandoo lauda laude lund lodu randi randiyon harami haramkhor bhadwa
        jhaat chod chodu chutiyaapa
        """,
    # No spaces between words: matched as substrings (see _SUBSTRING_LANGS).
    # The one-character terms (くそ's 糞, 씹, 좆, 屌) are left out on purpose:
    # the default min_len is two characters, so a one-character entry could
    # never match, and the CJK characters that mean something else inside an
    # ordinary compound (操 in 操场, 逼 in 逼真) would also start false
    # positives on the scripts that have no spaces to stop them.
    "ja": """
        ちんこ チンコ ちんぽ チンポ ちんちん まんこ マンコ おまんこ くそ クソ
        死ね ファック やりまん ヤリマン ビッチ くそったれ クソ野郎
        まんぐり返し くたばれ
        """,
    "ko": """
        씨발 시발 씨팔 좆같 좆같은 좆같이 개새끼 새끼 병신 지랄 창녀 걸레
        미친놈 미친년 엿먹어 개자식 후레자식 니미 존나 졸라
        """,
    "zh": """
        傻逼 傻屄 煞笔 妈的 他妈的 妈逼 操你 操你妈 草泥马 尼玛 鸡巴
        鸡巴毛 婊子 骚货 贱人 王八蛋 狗娘养的 屁眼 二逼 装逼 撕逼 肉棒
        撸管 杂种
        """,
    "zh-Hant": """
        傻逼 傻屄 幹你娘 幹你 機掰 雞掰 靠北 靠腰 雞巴 他媽的 媽的 媽逼
        操你媽 婊子 騷貨 賤人 王八蛋 狗娘養的 屁眼 雜種 肉棒 破麻
        """,
    "id": """
        kontol kontolmu memek memekmu ngentot ngentod entot bokep bajingan
        bangsat brengsek anjing anjingmu babi keparat sundal perek pantat
        coli jembut pepek jancok jancuk
        """,
    "vi": """
        đụ đù địt đéo cặc lồn buồi cứt đĩ điếm đm vãi cac lon
        """,
    "th": """
        เหี้ย ควย เย็ด เย็ดแม่ หี แม่ง สัส ชิบหาย อีดอก ดอกทอง ระยำ พ่อง แม่มึง
        ไอ้สัตว์ ปี้ กระหรี่ จังไร
        """,
    "fil": """
        putangina putanginamo puta putang tangina tanginamo gago gaga gagong
        tarantado kupal hindot kantot puki pekpek burat bayag punyeta pakyu
        hinayupak lintik
        """,
}

# Terms that are only profane as a phrase. A space in a term matches any run
# of punctuation or whitespace, so "hijo de puta" also sees "hijo-de-puta".
# A phrase is only worth listing when its own first word is not already a term
# ("nique ta mère" would never be reported — "nique" starts at the same place
# and is shorter).
_RAW_PHRASES: dict[str, tuple[str, ...]] = {
    "es": ("hijo de puta", "hija de puta", "hijo de perra", "a la verga"),
    "fr": ("fils de pute", "ta gueule", "va te faire foutre"),
    "pt": ("vai tomar no cu", "toma no cu"),
    "ru": ("иди нахуй", "твою мать"),
    "vi": ("mẹ kiếp", "chó đẻ", "con đĩ"),
}


def _fold(term: str) -> str:
    """The one spelling every comparison uses: NFKC, lowercase."""
    return unicodedata.normalize("NFKC", term).lower()


def _entries(lang: str) -> tuple[str, ...]:
    """This language's terms, folded, deduplicated, in the order listed."""
    return tuple(dict.fromkeys(
        [_fold(t) for t in _RAW_WORDS[lang].split()]
        + [_fold(t) for t in _RAW_PHRASES.get(lang, ())]))


_ORDERED: tuple[str, ...] = tuple(dict.fromkeys(
    term for lang in _RAW_WORDS for term in _entries(lang)))
WORDS_BY_LANG: dict[str, frozenset[str]] = {
    lang: frozenset(_entries(lang)) for lang in _RAW_WORDS}
del _RAW_WORDS, _RAW_PHRASES, _entries

# Every term the scan can report, deduplicated, in the order the lexicon lists
# them: the union across languages, so a track never has to say which language
# its lyrics are in. That order is also the order the matcher tries readings
# in, so two readings that tie report the English one.
_TERMS: tuple[str, ...] = _ORDERED
WORDS: frozenset[str] = frozenset(_TERMS)

# --------------------------------------------------------------------------- #
# Severity — the mild tier
# --------------------------------------------------------------------------- #
# Terms whose presence is MILD on its own: a lyric can carry one of these
# without being ABOUT anything explicit. A hit from this set is still reported
# (the readout says what was seen, and the AI stage above the scan reads it in
# context), but a track whose ONLY hits are these is not called explicit by the
# scan — "ass" in passing is not the evidence "motherfucker" is, and the scan
# exists to overrule a provider only when the words actually say so.
#
# Deliberately short, and the place to extend when a term turns out to fire on
# ordinary lyrics: the English "ass" family, and the body-word cognates that
# carry no insult of their own in the languages that have one. Insults BUILT on
# them stay strong ("asshole", "arschloch", "culero") — the compound is the
# insult, the body word is not.
_RAW_MILD = """
    ass asses arse arses asshat asshats jackass jackasses dumbass dumbasses
    culo culos culi arsch reet
"""
MILD: frozenset[str] = frozenset(_fold(t) for t in _RAW_MILD.split())
del _RAW_MILD


def strong(hits: Iterable[str]) -> list[str]:
    """The hits that establish explicit, in order: ``hits`` minus the mild tier.

    The filter every decision goes through, so "which terms count" lives in ONE
    place — a caller that reported ``hits`` and then asked ``if hits`` would
    count a lone "ass" as explicit, which is exactly what this tier is for.
    """
    return [h for h in hits if h not in MILD]

# Scripts that do not put spaces between words. A term in one of these is
# matched as a substring, because there is no token boundary to match against
# (and Korean glues particles onto the stem, which is the same problem).
_SUBSTRING_LANGS = frozenset({"ja", "ko", "zh", "zh-Hant", "th"})
_SUBSTRINGS: frozenset[str] = frozenset(
    term for lang in _SUBSTRING_LANGS for term in WORDS_BY_LANG[lang])


def normalize(text: str) -> str:
    """The folded, whitespace-collapsed form of ``text`` (lowercase, NFKC)."""
    return " ".join(unicodedata.normalize("NFKC", text).lower().split())


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
# One censored letter. An asterisk run stands in for exactly that many
# letters, which is what the censored spellings actually do: "f***ing" hides
# "uck", "ni**a" hides "gg", "sh*t" hides "i".
_ANY = "\x01"
# The combining marks the covered scripts attach to a letter: Devanagari vowel
# signs and nukta, Arabic harakat, Hebrew niqqud, Thai vowels and tone marks,
# and the general combining diacritics. They are not \w, so without this the
# boundary of a word would fall at its first vowel sign — "चूतिया" would answer
# as the shorter "चूत", and "गांडीव" (a bow, not a curse) would be read as one.
_MARK = (r"\u0300-\u036f\u0591-\u05c7\u0610-\u061a\u064b-\u065f\u0670"
         r"\u06d6-\u06ed\u0900-\u097f\u0e31\u0e34-\u0e3a\u0e47-\u0e4e")
# What may sit between two letters of a term: "f.u.c.k", "f_u_c.k", "f-u-c-k",
# "f u c k". Two characters is enough for every dodge that keeps the letters
# readable, and short enough that two ordinary words cannot be glued. The
# combining marks stay out of it even though they are in the boundary class:
# here they only ever separate letters inside a term that already matched,
# which is harmless, while their ranges — repeated between every pair of
# letters of every one of ~1300 branches — make the union five times as
# expensive to compile. A censored letter may be swallowed here too, and the
# best reading of a span then prefers the longer term ("f*ck" is "fuck", not
# the three-letter evasion spelling "fck").
_SEP = r"[\W_]{0,2}"
# Leetspeak letters, plus _ANY so a censored letter matches here too.
_LEET = {
    "a": "a4@", "b": "b8", "e": "e3", "g": "g9", "i": "i1!", "l": "l1|",
    "o": "o0", "s": "s5$", "t": "t7",
}
_STARS = re.compile(r"\*+")
# A letter drawn out for emphasis ("fuuuuck", "shiiit"): three or more of them
# collapse to one, so the lexicon only has to spell the word and no term can
# be read out of an ordinary doubled letter ("loon", "lull", "puuta"). The
# lexicon is asserted to carry no such run of its own.
_REPEATS = re.compile(r"(\w)\1{2,}")


def _term_pattern(term: str) -> str:
    """One lexicon term as a regex body: letters, leet, censoring, separators.

    Each letter is one character of its leet class or exactly one censored
    letter. Both are single characters, which is what makes a censored
    spelling readable: one asterisk per hidden letter, the way "f***ing" hides
    "uck" — and a drawn out spelling never needs to be spelled here, because
    `_prepare` has already collapsed its repeats.
    """
    parts = []
    for ch in term:
        if ch == " ":
            parts.append(_SEP)
            continue
        parts.append("[" + _LEET.get(ch, ch) + _ANY + "]")
    return _SEP.join(parts)


@lru_cache(maxsize=8)
def _matcher(terms: tuple[str, ...], min_len: int) -> "_Matcher":
    """The cached matcher for these terms (a tuple, so the cache can key it)."""
    words = tuple(t for t in terms if len(t) >= min_len and t not in _SUBSTRINGS)
    subs = tuple(t for t in terms if len(t) >= min_len and t in _SUBSTRINGS)
    return _Matcher(words, subs)


class _Matcher:
    """One pattern for the union of ``words``, plus the lookup that names a hit.

    The pattern has NO capture groups on purpose. A ~1300-branch alternation
    whose every branch is wrapped in a group loses CPython's alternation
    optimisation and scans a 50 KB text about 45x slower than the same
    alternation without groups; naming the match after the fact is free by
    comparison, because it happens once per HIT rather than once per word.
    """

    __slots__ = ("pattern", "words", "substrings", "_by_first", "_term_re")

    def __init__(self, words: tuple[str, ...], substrings: tuple[str, ...]):
        self.words = words
        self.substrings = substrings
        self.pattern = None
        self._by_first: dict[str, list[str]] = {}
        self._term_re: dict[str, re.Pattern[str]] = {}
        for term in words:
            first = term[0]
            for ch in _LEET.get(first, first) + _ANY:
                self._by_first.setdefault(ch, []).append(term)
        if words:
            # A censored letter is not a letter and not a boundary either:
            # without _ANY in the lookarounds, "f***" would also match the
            # shorter "f**" plus a stray asterisk, and "sh*t" would answer with
            # whatever other four-letter word happens to fit.
            bound = r"[\w" + _ANY + _MARK + r"]"
            src = (r"(?<!" + bound + r")(?:"
                   + "|".join(_term_pattern(t) for t in words)
                   + r")(?!" + bound + r")")
            self.pattern = re.compile(src)

    def best_at(self, text: str, start: int) -> tuple[str, int] | None:
        """The best reading of a term starting at ``start``: ``(term, end)``.

        "Best" is the reading that stretches over the fewest separators first —
        a term that is spelled out beats one that has to swallow a space
        between two words, so "fuck up" is the word "fuck" and not the compound
        "fuckup" — and the longest one after that, so that a censored or drawn
        out spelling is read as the whole word ("f***ing" is "fucking", not
        "fuck"), and "piss" is not the Danish three-letter "pis".

        Candidates are tried in matcher order (the lexicon's own order, English
        leading), which is what keeps the answer the same run to run when two
        readings tie on both counts.
        """
        best: tuple[str, int] | None = None
        best_key: tuple[int, int] | None = None
        for term in self._by_first.get(text[start], ()):
            rx = self._term_re.get(term)
            if rx is None:
                rx = self._term_re[term] = re.compile(_term_pattern(term))
            match = rx.match(text, start)
            if match is None:
                continue
            span = match.group(0)
            key = (_separators(span), -len(span))
            if best_key is None or key < best_key:
                best, best_key = (term, match.end()), key
        return best


def _prepare(text: str) -> str:
    """Folded text: drawn out letters collapsed, asterisk runs censored.

    The input is a lyric, so the text is worth normalizing here once: NFKC
    folds the full-width spellings a CJK site prints, lowercase makes the
    comparison case-insensitive, a sentinel that arrived in the input is not a
    censored letter, three or more of a letter are one letter, and an asterisk
    run becomes one censored letter per asterisk.
    """
    folded = unicodedata.normalize("NFKC", text).lower().replace(_ANY, "")
    folded = _REPEATS.sub(r"\1", folded)
    return _STARS.sub(lambda m: _ANY * len(m.group(0)), folded)


def _separators(span: str) -> int:
    """How many characters of a matched span were separators, not letters.

    A censored letter and a combining mark are not separators: both belong to
    the term, and charging for them would make an honest match look stretched.
    """
    return sum(1 for ch in span
               if not (ch.isalnum() or ch == "_" or ch == _ANY
                       or unicodedata.category(ch).startswith("M")))


_CACHE: dict[tuple, tuple[str, ...]] = {}
_CACHE_MAX = 512


def scan(text: str, *, extra: Iterable[str] = (), min_len: int = 2) -> list[str]:
    """Every term from the lexicon that occurs in ``text``, distinct.

    The result is in first-appearance order (two terms that start at the same
    place report the longer one first). ``extra`` adds the caller's own terms
    for this call — matched with the same tolerance as the lexicon — and
    ``min_len`` raises the shortest term that may match at all.
    """
    if not text:
        return []
    extras = tuple(sorted({_fold(t).strip() for t in extra if t and t.strip()}))
    key = (text, extras, min_len)
    cached = _CACHE.get(key)
    if cached is not None:
        return list(cached)

    prepared = _prepare(text)
    hits: dict[str, int] = {}
    # The lexicon first, then the caller's terms: a term in both lists is the
    # same hit, and `setdefault` keeps the first position either way.
    for terms in ((_TERMS, extras) if extras else (_TERMS,)):
        matcher = _matcher(terms, min_len)
        for term in matcher.substrings:
            pos = prepared.find(term)
            if pos >= 0:
                hits.setdefault(term, pos)
        # The pattern finds where a term may start and where it may end; the
        # best reading of that place names the term and says how far it goes.
        pattern = matcher.pattern
        if pattern is not None:
            pos = 0
            while True:
                match = pattern.search(prepared, pos)
                if match is None:
                    break
                span = match.group(0)
                pos = match.end()
                if not any(ch.isalpha() for ch in span):
                    # Nothing but censorship: "**" hides a word, it does not
                    # spell one, and reading a two-letter term out of it is how
                    # a stray asterisk would make a track explicit on its own.
                    continue
                best = matcher.best_at(prepared, match.start())
                if best is not None:
                    term, end = best
                    hits.setdefault(term, match.start())
                    pos = end

    # Sorted by position; the two tie-breakers only order terms that start at
    # the same character, and they keep the answer identical run to run.
    found = tuple(term for term, _pos in
                  sorted(hits.items(), key=lambda kv: (kv[1], -len(kv[0]), kv[0])))
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()   # bounded: a scored album is tens of entries
    _CACHE[key] = found
    return list(found)


# --------------------------------------------------------------------------- #
# Lyrics
# --------------------------------------------------------------------------- #
# LRC metadata ([ar:…], [ti:…], [offset:…]) and the credit lines a provider
# prepends ("Lyrics: …", "作词 : …") are not lyrics and must not be scanned —
# a "[ti:Shitty Song]" would otherwise be the only reason to call a track
# explicit.
_LRC_META = re.compile(
    r"^\[(?:ar|ti|al|au|by|re|ve|length|offset|tool|version|encoding)\s*:[^\]]*\]$",
    re.IGNORECASE)
_CREDIT = re.compile(
    r"^(?:lyrics?|written by|composed by|produced by|translated by|credits?"
    r"|作词|作曲|编曲|翻译)\s*[:：]\s*\S.*$",
    re.IGNORECASE)
# [mm:ss.xx] / [mm:ss.xxx] / [mm:ss] / <mm:ss.xx> (Enhanced LRC word stamps).
_TIMESTAMP = re.compile(r"[\[<]\s*\d{1,3}\s*:\s*\d{1,2}(?:[.:]\d{1,3})?\s*[\]>]")
# [Chorus], [Verse 1], [x2] — a section header, never a lyric.
_SECTION = re.compile(r"\[[^\]\n]{1,40}\]")


def _strip_lyrics(text: str) -> str:
    """Drop LRC/credit lines and every timestamp or section header."""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or _LRC_META.match(stripped) or _CREDIT.match(stripped):
            continue
        line = _SECTION.sub(" ", _TIMESTAMP.sub(" ", line))
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def scan_lyrics(text: str, *, extra: Iterable[str] = ()) -> list[str]:
    """``scan`` for a lyric file: LRC scaffolding and headers are not lyrics."""
    return scan(_strip_lyrics(text), extra=extra)
