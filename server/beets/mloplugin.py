"""la musica beets plugin (``mloplugin``).

Adds Picard-parity behaviors on top of a vanilla beets import:

* ``mlo_dir`` album template field — evaluates MLO's Picard-style naming
  script (server/naming.py) so beets organizes files into the exact same
  folder structure the MLO organizer produces.
* MusicBrainz alias translations for a preferred locale (Picard's
  "Translate titles/names to preferred locale"): rewrites TITLE / ARTIST /
  ALBUMARTIST to the locale alias and fills the matching SORT tags with
  the alias sort names.
* Work relationships — recordings linked to a work in MusicBrainz get
  WORK (and MOVEMENT when the work is a movement/part).
* Release-type capitalization: ``ep`` → ``EP``, others Title Case
  (Picard's common $map release-type script).

The plugin edits files through MLO's own tag layer (mlo.audio), so every
container-specific tag mapping (vorbis / ID3 TXXX / MP4 freeform) matches
what the rest of la musica writes.

This module runs inside the beets process, which is the same Python
interpreter with the repo root on sys.path (set up below).
"""
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

from beets import plugins
from beets.plugins import BeetsPlugin

# Make server.* / mlo.* importable: this file lives at <repo>/server/beets/.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

UA = "MusicLibraryOptimizer/2.0 (beets mloplugin)"
_MB_LOCK = threading.Lock()
_MB_LAST = 0.0
_MB_CACHE = {}


def mb_get(path, params):
    """Rate-limited (1 req/s) MusicBrainz ws/2 GET with an in-process cache."""
    global _MB_LAST
    key = (path, tuple(sorted(params.items())))
    with _MB_LOCK:
        if key in _MB_CACHE:
            return _MB_CACHE[key]
        wait = 1.0 - (time.time() - _MB_LAST)
        if wait > 0:
            time.sleep(wait)
        url = ("https://musicbrainz.org/ws/2" + path + "?" +
               urllib.parse.urlencode(params))
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            _MB_LAST = time.time()
            return {}
        _MB_LAST = time.time()
        _MB_CACHE[key] = data
        return data


def _locale_alias(entity_type, mbid, locale):
    """(name, sort_name) of the best alias in *locale*, or None.

    Preference: locale + primary flag, then any alias in the locale.
    """
    if not mbid:
        return None
    data = mb_get(f"/{entity_type}/{mbid}", {"inc": "aliases", "fmt": "json"})
    best = None
    for a in data.get("aliases", []):
        if locale and str(a.get("locale") or "").lower() != locale.lower():
            continue
        cand = (str(a.get("name") or "").strip(), str(a.get("sort-name") or "").strip(),
                bool(a.get("primary")))
        if not cand[0]:
            continue
        if best is None or (cand[2] and not best[2]):
            best = cand
    if best is None:
        return None
    return best[0], best[1]


# Scripts that are not Latin: CJK, Hangul, Kana, Cyrillic, Greek, Hebrew,
# Arabic, Devanagari, Thai.
_NON_LATIN_RE = re.compile(
    "[\u0370-\u03ff\u0400-\u04ff\u0590-\u05ff\u0600-\u06ff\u0900-\u097f"
    "\u0e00-\u0e7f\u1100-\u11ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
    "\uf900-\ufaff\uac00-\ud7af]")
# Locales whose own names are written in Latin script.
_LATIN_LOCALES = {
    "en", "de", "fr", "es", "it", "pt", "nl", "sv", "no", "da", "fi", "is",
    "pl", "cs", "sk", "hu", "ro", "tr", "vi", "id", "ms", "tl", "hr", "sl",
    "lt", "lv", "et", "ca", "gl", "eu", "af", "sq",
}


def _locale_lookup_needed(value, locale):
    """False when a locale-alias lookup cannot change *value* anyway.

    The alias lookup exists to TRANSLATE a name into the reader's locale
    (Picard's "translate titles/names to preferred locale"). A name already
    written in that locale's script has nothing to translate, but the lookup
    still costs one of MusicBrainz' rate-limited requests — and this stage ran
    three of them per track, so a Latin-script library spent more MB time in
    the plugin than beets' own import did. Beets itself fills the sort tags
    from MB's sort-names, so skipping loses nothing there either.
    """
    if not value:
        return False
    if locale.split("-")[0].lower() in _LATIN_LOCALES and not _NON_LATIN_RE.search(value):
        return False
    return True


def _work_for_recording(recording_mbid):
    """(work_title, work_type) from the recording's performance->work rel."""
    if not recording_mbid:
        return None
    data = mb_get(f"/recording/{recording_mbid}",
                  {"inc": "work-rels", "fmt": "json"})
    for rel in data.get("relations", []):
        work = rel.get("work")
        if not work:
            continue
        title = str(work.get("title") or "").strip()
        if title:
            return title, str(work.get("type") or "").strip()
    return None


def _cap_releasetypes(types):
    """Picard release-type script: EP uppercased, everything else Title Case."""
    out = []
    for t in types or []:
        t = str(t).strip()
        if not t:
            continue
        out.append("EP" if t.lower() == "ep" else t.title())
    return out


def _date_str(y, m, d):
    y = int(y or 0)
    if not y:
        return ""
    if int(m or 0) and int(d or 0):
        return f"{y:04d}-{int(m):02d}-{int(d):02d}"
    if int(m or 0):
        return f"{y:04d}-{int(m):02d}"
    return f"{y:04d}"


def _first(value):
    """Flatten beets list fields (label, country, artists...) to a scalar."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else ""
    return value or ""


def _multi(value):
    """MLO's own spelling of a multi-value field: entries joined with '; '.

    mlo.naming reads ARTIST / ALBUMARTIST / RELEASETYPE as the stored
    string (the tag layer joins several comments with '; '), so taking only
    beets' first entry would make the two sides compute DIFFERENT folder
    names for a release with several artists or types.
    """
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v).strip() for v in value if str(v).strip())
    return value or ""


def _multi_first(value):
    """First entry of a field MLO reduces to one value (RELEASECOUNTRY,
    LABEL — see mlo.naming._first_multi, whose rule is reused here so both
    sides split "US; GB", "EU / UK" and "A + B" identically)."""
    from mlo.naming import _first_multi

    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return _first_multi(value)


def _item_track_file(item):
    """Track filename in MLO's D-TT convention: '1-01 Song.flac'-style
    (unpadded disc, zero-padded track) - beets' own $disc pads the disc
    to two digits, which the MLO grader/library don't expect.

    Only used when the naming script has no filename segment of its own
    (see _item_mlo_file); the shipped default does have one.
    """
    try:
        disc = int(item.disc or 1)
    except (TypeError, ValueError):
        disc = 1
    try:
        track = int(item.track or 0)
    except (TypeError, ValueError):
        track = 0
    title = str(item.title or "").strip()
    return f"{disc}-{track:02d} {title}"


def _item_naming_vars(item):
    """MLO naming-script variables from a beets item's (denormalized) tags.

    Key-for-key the map mlo.naming.track_variables builds from a file's
    tags, and with the same multi-value rules — the two are evaluated
    against the same naming script and must agree on every variable.
    """
    return {
        "albumartist": _multi(item.albumartist),
        "artist": _multi(item.artist),
        "albumartistsort": _multi(item.albumartist_sort),
        "musicbrainz_albumartistid": _first(item.mb_albumartistid) or "",
        "musicbrainz_artistid": _first(item.mb_artistid) or "",
        "musicbrainz_albumid": _first(item.mb_albumid) or "",
        # The two track ids the script may name a file with: the RECORDING
        # (mb_trackid) and this release's track (mb_releasetrackid).
        "musicbrainz_trackid": _first(item.mb_trackid) or "",
        "musicbrainz_releasetrackid": _first(item.mb_releasetrackid) or "",
        # Capitalized exactly as the plugin's own release_type_caps pass
        # writes the tag, so the name beets computes now still matches the
        # tag the next run reads (and the MLO organizer evaluates).
        "releasetype": "; ".join(
            _cap_releasetypes(getattr(item, "albumtypes", None)
                              or getattr(item, "releasetype", None))),
        "originaldate": _date_str(item.original_year, item.original_month, item.original_day),
        "date": _date_str(item.year, item.month, item.day),
        "year": f"{item.year:04d}" if item.year else "",
        "originalyear": f"{item.original_year:04d}" if item.original_year else "",
        "album": _first(item.album) or "",
        "releasecountry": _multi_first(item.country),
        "media": _first(item.media) or "",
        "catalognumber": _first(item.catalognum) or "",
        "label": _multi_first(item.label),
        "discnumber": str(item.disc or 1),
        "disctotal": str(item.disctotal or ""),
        "tracknumber": str(item.track or ""),
        "tracktotal": str(item.tracktotal or ""),
        "title": _first(item.title) or "",
        "genre": _first(item.genre) or "",
    }


def _item_mlo_path(item):
    """Full relative path (album folder + filename) from MLO's naming script.

    The script is the single source of truth for file structure: evaluating
    it here is what makes a beets import land exactly where the MLO
    organizer and the grader's naming check expect — ids, brackets and all.
    """
    try:
        from server.naming import DEFAULT_NAMING_SCRIPT, eval_script
        from mlo.config import load_config
        cfg = load_config()
        script = (cfg.get("naming_script") or "").strip() or DEFAULT_NAMING_SCRIPT
        shorter = bool(cfg.get("short_folder_names", False))
        return eval_script(script, _item_naming_vars(item), shorter_ids=shorter)
    except Exception as e:  # noqa: BLE001 - never break an import over paths
        print(f"[mloplugin] naming-script evaluation failed: {e}", file=sys.stderr)
        return ""


def _item_mlo_dir(item):
    """Album folder: every segment the naming script produces but the last."""
    parts = [p for p in _item_mlo_path(item).split("/") if p]
    if len(parts) > 1:
        return "/".join(parts[:-1])
    if parts:
        return parts[0]
    # Fallback: beets' classic artist/album layout.
    artist = _first(item.albumartist) or "Unknown Artist"
    name = _first(item.album) or "Unknown Album"
    return f"{artist}/{name}"


def _item_mlo_file(item):
    """Track filename: the naming script's last segment.

    Taken from the script (not rebuilt here) so the ids, padding and
    brackets a beets import writes are the ones the MLO grader verifies.
    """
    parts = [p for p in _item_mlo_path(item).split("/") if p]
    if len(parts) > 1:
        return parts[-1]
    return _item_track_file(item)


# The item currently being routed; set by the $mlo_file field evaluation,
# read by the $mlo_dir template function (single-threaded import).
_current_item = {"item": None}


def _mlo_file_field(item):
    _current_item["item"] = item
    return _item_mlo_file(item)


def _mlo_dir_func(*args):
    """$mlo_dir{$mlo_file} - album directory (from MLO's naming script)
    joined with the track filename. Template functions are the only way to
    emit real path separators: field VALUES get their separators flattened."""
    item = _current_item["item"]
    if item is None:
        return "/".join(a for a in args if a)
    try:
        album_dir = _item_mlo_dir(item)
    except Exception as e:  # noqa: BLE001
        print(f"[mloplugin] path evaluation failed: {e}", file=sys.stderr)
        album_dir = ""
    if args:
        return "/".join([album_dir] + [a for a in args if a])
    return album_dir


class MloPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self.config.add({
            "locale": "en",
            "translations": True,
            "work_movement": True,
            "release_type_caps": True,
        })
        # Per-instance maps (BeetsPlugin.__init__ creates them).
        # $mlo_file caches the routed item; $mlo_dir{$mlo_file} then yields
        # the full MLO-convention relative path for the track.
        self.template_fields["mlo_file"] = _mlo_file_field
        self.template_funcs["mlo_dir"] = _mlo_dir_func
        self.register_listener("album_imported", self.on_album_imported)

    def get_import_stages(self):
        return [self._translate_stage]

    def _translate_stage(self, session, task):
        """Locale translations as an import stage: runs BEFORE beets writes
        tags and computes destinations, so the aliased names land in the
        tags AND in the organized folder paths (Picard's behavior)."""
        locale = str(self.config["locale"].get() or "").strip()
        if not (self.config["translations"].get(True) and locale):
            return
        for item in task.imported_items():
            try:
                if _locale_lookup_needed(item.title, locale):
                    alias = _locale_alias("recording", str(item.mb_trackid or ""), locale)
                    if alias and item.title != alias[0]:
                        self._log.debug("title alias: {0} -> {1}", item.title, alias[0])
                        item.title = alias[0]
                        item.title_sort = alias[1] or alias[0]
                if _locale_lookup_needed(item.artist, locale):
                    alias = _locale_alias("artist", str(item.mb_artistid or ""), locale)
                    if alias and item.artist != alias[0]:
                        item.artist = alias[0]
                        item.artist_sort = alias[1] or alias[0]
                if _locale_lookup_needed(item.albumartist, locale):
                    alias = _locale_alias("artist", str(item.mb_albumartistid or ""), locale)
                    if alias and item.albumartist != alias[0]:
                        item.albumartist = alias[0]
                        item.albumartist_sort = alias[1] or alias[0]
            except Exception as e:  # noqa: BLE001
                self._log.error("alias lookup failed for {0}: {1}", item.path, e)

    # ------------------------------------------------------------------
    def on_album_imported(self, lib, album):
        do_work = bool(self.config["work_movement"].get(True))
        do_caps = bool(self.config["release_type_caps"].get(True))

        try:
            from mlo.audio import AudioFile
        except Exception as e:  # noqa: BLE001
            self._log.error("mlo.audio unavailable: {0}", e)
            return

        # beets 2.x names the release-type field "albumtypes" (a list).
        types = (getattr(album, "albumtypes", None)
                 or getattr(album, "releasetype", None))
        capped = _cap_releasetypes(types) if do_caps else None

        for item in album.items():
            path = item.path
            if isinstance(path, bytes):
                path = path.decode("utf-8", "replace")
            try:
                af = AudioFile(path)
                if af.audio is None:
                    continue
            except Exception:  # noqa: BLE001
                continue

            changed = False

            # 1) Release-type capitalization
            if capped:
                value = "; ".join(capped)
                if value and str(af.get_tag("RELEASETYPE") or "").strip() != value:
                    af.set_tag("RELEASETYPE", value)
                    changed = True

            # 2) Classical works / movements
            if do_work:
                try:
                    found = _work_for_recording(str(item.mb_trackid or ""))
                    if found:
                        title, wtype = found
                        tag = "MOVEMENT" if wtype.lower() in ("movement", "part") else "WORK"
                        if str(af.get_tag(tag) or "").strip() != title:
                            af.set_tag(tag, title)
                            changed = True
                except Exception as e:  # noqa: BLE001
                    self._log.warning("work-rels lookup failed for {0}: {1}", path, e)

            # 3) Original release date (release-group first release). Beets
            #    knows it and the naming script uses it for folder names,
            #    but beets never writes the tag itself — Picard does.
            orig = _date_str(item.original_year, item.original_month, item.original_day)
            if orig and str(af.get_tag("ORIGINALDATE") or "").strip() != orig:
                af.set_tag("ORIGINALDATE", orig)
                changed = True
            orig_year = f"{item.original_year:04d}" if item.original_year else ""
            if orig_year and str(af.get_tag("ORIGINALYEAR") or "").strip() != orig_year:
                af.set_tag("ORIGINALYEAR", orig_year)
                changed = True

            if changed:
                self._log.info("mloplugin touched tags: {0}", os.path.basename(path))
