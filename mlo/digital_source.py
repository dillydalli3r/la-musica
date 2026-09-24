"""Where a digital release's ``SOURCE`` comes from — the one honest answer.

The grader requires a non-empty, uniform ``SOURCE`` on every track of an album
whose ``MEDIA`` is Digital Media (``grade_check_source``, spec §3), and the app
writes exactly three values of its own (``mlo.tagtext.SOURCE_VALUES``:
"Soulseek" on a download, "YouTube" on the video branch, "Digital" as the
MEDIA/SOURCE pass's configured fallback). Everything else is the user's own
word, and this module is where the IMPORT looks for one it may write without
inventing it.

The evidence it reads, in order:

* **The release's own URLs.** A MusicBrainz release carries url-relations —
  "purchase for download", "download for free", "streaming" — and the host
  names the store the release is published at (bandcamp.com, qobuz.com,
  open.spotify.com, …). That is a fact about the RELEASE, which is why it may
  be written: it is where this pressing comes from, not a guess about who
  ripped it.
* **The provider the acquisition itself knew** ("Soulseek", "YouTube"): the
  download path states its own answer and hands it in here.

Neither answers → nothing is written and the import ASKS (the ``source`` family
in ``mlo.import_policy`` carries the gap, the wizard's Match step the control).
A value this table has never heard of is returned unchanged, exactly as
``mlo.tagtext``'s closed-vocabulary rule says: SOURCE is otherwise the user's
own word, and a store this list has not learned yet must keep its spelling
rather than be coerced.

Pure and stdlib-only: the import path, the wizard's route and the tests all
read ONE table.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional, Tuple
from urllib.parse import urlsplit

__all__ = ["STORE_SOURCES", "source_from_url", "source_from_urls",
           "source_from_release", "KNOWN_SOURCES"]

# host suffix -> the SOURCE value the store is called, in the app's own
# spelling. Matched on the HOST (a subdomain of the suffix counts: both
# "www.bandcamp.com" and "artist.bandcamp.com" are Bandcamp), never on a path,
# and case-insensitively. Keep the list to stores that really SELL or stream
# digital releases: an entry here is a value the import may write unprompted.
STORE_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("bandcamp.com", "Bandcamp"),
    ("qobuz.com", "Qobuz"),
    ("deezer.com", "Deezer"),
    ("spotify.com", "Spotify"),
    ("apple.com", "Apple Music"),          # music.apple.com / itunes.apple.com
    ("itunes.apple.com", "Apple Music"),
    ("amazon.com", "Amazon Music"),        # plus the regional hosts below
    ("amazon.co.uk", "Amazon Music"),
    ("amazon.de", "Amazon Music"),
    ("amazon.co.jp", "Amazon Music"),
    ("amazon.fr", "Amazon Music"),
    ("amazon.it", "Amazon Music"),
    ("amazon.es", "Amazon Music"),
    ("amazon.ca", "Amazon Music"),
    ("amazon.com.au", "Amazon Music"),
    ("tidal.com", "Tidal"),
    ("7digital.com", "7digital"),
    ("junodownload.com", "Juno Download"),
    ("beatport.com", "Beatport"),
    ("hdtracks.com", "HDtracks"),
    ("prostudiomasters.com", "ProStudioMasters"),
    ("soundcloud.com", "SoundCloud"),
    ("youtube.com", "YouTube"),            # youtube.com / music.youtube.com
    ("youtu.be", "YouTube"),
    ("music.apple.com", "Apple Music"),
    ("archive.org", "Internet Archive"),
    ("ototoy.jp", "OTOTOY"),
    ("mora.jp", "mora"),
    ("e-onkyo.com", "e-onkyo music"),
    ("prestomusic.com", "Presto Music"),
    ("boomkat.com", "Boomkat"),
    ("napster.com", "Napster"),
    ("traxsource.com", "Traxsource"),
    ("bleep.com", "Bleep"),
    ("store.tidal.com", "Tidal"),
)

# The values this table can produce, deduplicated, in table order — what a
# caller may offer as a pick list (`mlo.tagtext.SOURCE_VALUES` stays the
# vocabulary the app WRITES on its own; these are the store names it can
# recognize).
KNOWN_SOURCES: Tuple[str, ...] = tuple(
    dict.fromkeys(label for _host, label in STORE_SOURCES))

# A URL, or nothing: the providers state a resource and some of them are
# relative ("/release/…"), which names no store.
_URL_RE = re.compile(r"^(?:https?://)?([^/?#]+)", re.IGNORECASE)


def source_from_url(url: str) -> str:
    """The store *url* names, or "".

    The HOST decides, by suffix: "https://shop.bandcamp.com/album/x" and
    "http://bandcamp.com/album/x" are both Bandcamp, and a URL whose host
    matches nothing (a fan site, a forum thread) states no source.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        host = (urlsplit(raw if "//" in raw else "//" + raw).hostname or "").lower()
    except ValueError:
        match = _URL_RE.match(raw)
        host = (match.group(1) if match else "").lower().split(":")[0]
    if not host:
        return ""
    for suffix, label in STORE_SOURCES:
        if host == suffix or host.endswith("." + suffix):
            return label
    return ""


def source_from_urls(urls: Optional[Iterable[str]]) -> str:
    """The first URL in *urls* that names a known store, or "".

    Deterministic: the caller's order decides, which lets it rank (a
    "purchase for download" relation before a "streaming" one) without this
    table having to.
    """
    for url in urls or ():
        label = source_from_url(url)
        if label:
            return label
    return ""


def _release_urls(release: dict) -> list:
    """Every URL a release payload states, in a deterministic order.

    Three shapes reach here: the ``relations`` of a raw MusicBrainz payload
    (``[{"type": "purchase for download", "url": {"resource": …}}]``), a plain
    ``urls``/``links`` list, and the ``url`` a caller resolved itself. A
    purchase/download relation outranks a streaming one — the same release on
    Bandcamp and Spotify was BOUGHT at Bandcamp for the bytes in hand — and
    MB's own order decides within a rank.
    """
    rel = release or {}
    ranked = {"purchase for download": 0, "download for free": 1,
              "free streaming": 2, "streaming": 3}
    rows = []
    for i, item in enumerate(rel.get("relations") or ()):
        if not isinstance(item, dict):
            continue
        url = str(((item.get("url") or {}).get("resource")
                   if isinstance(item.get("url"), dict) else item.get("url")) or "").strip()
        if url:
            rows.append((ranked.get(str(item.get("type") or "").lower(), 4), i, url))
    out = [url for _r, _i, url in sorted(rows)]
    for key in ("urls", "links"):
        value = rel.get(key)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, (list, tuple)):
            out.extend(str(u) for u in value if u)
    for key in ("url", "source_url", "purchase_url"):
        if rel.get(key):
            out.append(str(rel[key]))
    return out


def source_from_release(release: Optional[dict]) -> str:
    """The store a release's own payload names, or "".

    *release* is whatever the caller has: a raw MusicBrainz release payload, a
    ``release_lookup`` dict, or the row the wizard's search returned. A
    ``provider``/``source`` key a caller already resolved wins — it is the
    acquisition's own statement, which beats an inference from the release's
    URLs.
    """
    rel = release or {}
    for key in ("source", "provider", "acquisition"):
        value = str(rel.get(key) or "").strip()
        if value:
            return value
    return source_from_urls(_release_urls(rel))
