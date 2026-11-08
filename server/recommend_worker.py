"""Recommendations → acquisition: queue missing albums by artists you play.

The listener's own top artists (the play store's last month, falling back to
all time) are asked, through MusicBrainz's artist browse, which release groups
the library does NOT already hold; the newest ALBUM/EP among them is queued
through the SAME "add to library" path an artist watch uses — a framework
album, its wish, and the existing wish worker searching it. Nothing is
downloaded here.

What it deliberately does NOT do:

* it never queues a release group the library owns, a wish already holds, or
  the artist watch has already acted on (`owned_mbids`,
  `queued_release_groups`, `handled_release_groups` are the ONE duplicate rule);
* it never queues a type outside the artist watch's own album/EP selection
  (`mlo.release_choice.type_matches`), so a live record, compilation or score
  is not swept up as "another album";
* it never queues more than `recommendation_auto_acquire_limit` albums in one
  cycle, across all artists — the anti-dump cap;
* it does nothing while `recommendation_auto_acquire_enabled` is off or the
  `auto_acquisition_enabled` master switch is off.

An album is only considered for an artist the person actually plays: the top
`recommendation_auto_acquire_artists` of the play store, not a chart, not a
genre list. That is what makes "more of what you love" a recommendation rather
than a storefront.

The cycle is on its own thread and persists its last run in
``<music>/.mlo/data/recommend_acquire.json``, so a restart does not re-run it
immediately; a failed cycle records the error and tries again on the next tick.
"""
import json
import os
import tempfile
import threading
import time
import traceback

from mlo import import_policy

_TICK_S = 900
_SETTLE_S = 60.0

_lock = threading.RLock()
_stop = threading.Event()
_thread = None
_running_cycle = False

_state = {
    "last_run": 0.0,
    "last_result": "",
    "queued": [],
    "running": False,
    "current": [],
}


def _state_path():
    """<music>/.mlo/data/recommend_acquire.json, beside the other stores."""
    from mlo.paths import app_data_dir
    try:
        data = app_data_dir()
    except Exception:
        data = ""
    return os.path.join(data, "recommend_acquire.json") if data else ""


def _load_state():
    path = _state_path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_state():
    path = _state_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".recommend_acquire_",
                                   suffix=".json",
                                   dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({
                "last_run": float(_state.get("last_run") or 0),
                "last_result": str(_state.get("last_result") or "")[:400],
                "queued": list(_state.get("queued") or [])[:50],
            }, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
        tmp = None
    except Exception:
        pass
    finally:
        try:
            if 'tmp' in locals() and tmp and os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _set(**kw):
    with _lock:
        _state.update(kw)


def status():
    """The worker's last run and when the next one is due (for a page/tray)."""
    from mlo import load_config
    cfg = load_config()
    interval = int(cfg.get("recommendation_auto_acquire_interval_hours", 24) or 24) * 3600
    with _lock:
        out = dict(_state)
    out["enabled"] = bool(cfg.get("recommendation_auto_acquire_enabled", True))
    out["interval_hours"] = interval // 3600
    out["next_run"] = (float(out.get("last_run") or 0) + interval) if out.get("last_run") else 0
    return out


def _artist_mbid(artist, albums):
    """The artist's MusicBrainz id, read off the library's own tags.

    The library payload's artist row carries no MBID of its own (an artist is a
    FOLDER, and two of its releases could theoretically disagree). The release
    identity the files were imported with is the authoritative source: the
    album's ALBUMARTIST id first, then any track's ARTIST id. No request is
    made here.
    """
    for album in albums or []:
        meta = album.get("meta") or {}
        for key in ("MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_ALBUMARTISTID"):
            value = str(meta.get(key) or "").strip()
            if value:
                return value
        for track in album.get("tracks") or []:
            tags = track.get("tags") or {}
            for key in ("MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_ALBUMARTISTID"):
                value = str(tags.get(key) or "").strip()
                if value:
                    return value
    return ""


def _top_artists(cfg, limit):
    """The listener's most-played artists, this month falling back to all time.

    The play store's own chart is the source: a track that was merely scrobbled
    elsewhere does not name an artist here, and an album played twice today
    outranks one played twice years ago. When both windows are empty the cycle
    has nothing to recommend and says so.
    """
    from server import plays as plays_store

    for period in ("month", "all"):
        try:
            payload = plays_store.top(period=period, kind="artists", limit=limit,
                                      user="", cfg=cfg)
        except Exception:
            continue
        rows = payload.get("items") or []
        if rows:
            return rows
    return []


def _library_artists(cfg):
    """{artist name casefold: (artist row, mbid)} from the current library."""
    from server import library as lib_mod

    lib = lib_mod.build_library(cfg)
    out = {}
    for artist in lib.get("artists") or []:
        name = str(artist.get("display_name") or artist.get("name") or "").strip()
        if not name:
            continue
        out[name.casefold()] = (artist, _artist_mbid(artist, artist.get("albums")))
        # The folder basename is how the play chart names an artist when the
        # tags carry no display name.
        out.setdefault(str(artist.get("name") or "").casefold(), out[name.casefold()])
    return out


def run_cycle(force=False, cfg=None):
    """One acquisition cycle. Returns a report; never raises."""
    global _running_cycle
    from mlo import load_config

    cfg = cfg or load_config()
    out = {"ok": True, "checked": 0, "queued": [], "errors": [],
           "summary": "", "artists": []}
    if not cfg.get("recommendation_auto_acquire_enabled", True):
        out["summary"] = "recommendation acquisition is off"
        return out
    if not import_policy.auto_acquisition_enabled(cfg):
        out["summary"] = import_policy.AUTO_OFF_NOTE
        return out
    interval = int(cfg.get("recommendation_auto_acquire_interval_hours", 24) or 24) * 3600
    with _lock:
        last = float(_state.get("last_run") or 0)
        if not force and last and (time.time() - last) < interval:
            out["summary"] = "not due"
            return out
        if _running_cycle:
            out["summary"] = "a cycle is already running"
            return out
        _running_cycle = True
        _set(running=True, current=[])
    try:
        from server import artist_watch

        cap = max(1, int(cfg.get("recommendation_auto_acquire_limit", 3) or 3))
        artist_cap = max(1, int(cfg.get("recommendation_auto_acquire_artists", 4) or 4))
        wanted = ("album", "ep")
        tops = _top_artists(cfg, artist_cap)
        if not tops:
            out["summary"] = ("no play history yet — nothing to recommend "
                              "an acquisition from")
            with _lock:
                _state.update({"last_run": time.time(),
                               "last_result": out["summary"]})
            _save_state()
            return out
        library = _library_artists(cfg)
        out["artists"] = [str(r.get("name") or "") for r in tops]
        owned = artist_watch.owned_mbids(cfg)
        queued = artist_watch.queued_release_groups()
        handled = artist_watch.handled_release_groups()
        seen = set()
        _set(current=[str(r.get("name") or "") for r in tops])
        for row in tops:
            if len(out["queued"]) >= cap:
                break
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            hit = library.get(name.casefold())
            mbid = (hit[1] if hit else "") or ""
            if not mbid:
                # A library row whose files carry no artist id: resolve the
                # NAME once (MusicBrainz's own search). This is the only
                # network request made for an artist with no id.
                try:
                    from server import discovery
                    mbid = discovery.resolve_artist_mbid(name, cfg=cfg,
                                                         exact_only=True) or ""
                except Exception as e:
                    out["errors"].append(f"{name}: could not resolve the artist ({e})")
                    continue
            if not mbid:
                out["errors"].append(f"{name}: no MusicBrainz artist id")
                continue
            try:
                groups = artist_watch.release_groups(mbid)
            except Exception as e:
                out["errors"].append(f"{name}: MusicBrainz did not answer ({e})")
                continue
            out["checked"] += 1
            # A browse can name hundreds of release groups; the cap is on
            # QUEUED albums, not on how many editions one cycle asks the
            # release-choice policy about, so the examination is bounded too.
            examined = 0
            artist_queued = 0
            for group in groups:
                if len(out["queued"]) >= cap or examined >= 30 or artist_queued:
                    break
                examined += 1
                gid = str(group.get("id") or "").strip().lower()
                if not gid or gid in seen:
                    continue
                seen.add(gid)
                if gid in owned or gid in queued or gid in handled:
                    continue
                if not artist_watch.type_matches(
                        group.get("primary_type"),
                        group.get("secondary_types") or (),
                        wanted):
                    continue
                title = str(group.get("title") or "").strip()
                date = str(group.get("first_release_date") or "")
                year = date[:4] if len(date) >= 4 else ""
                release, err, pick = artist_watch._release_for(gid, cfg,
                                                               types=wanted)
                if err or not release:
                    out["errors"].append(f"{name} — {title}: {err or 'no edition'}")
                    continue
                try:
                    created = artist_watch.queue_release(
                        release, cfg, title=title, artist=name, year=year,
                        candidates=(pick or {}).get("candidates"))
                except Exception as e:
                    out["errors"].append(f"{name} — {title}: {e}")
                    continue
                if not created.get("created"):
                    # Already owned/queued after all (a race with a manual
                    # add): not an error, and not a second copy.
                    continue
                out["queued"].append({
                    "title": title, "artist": name, "year": year,
                    "release_group_mbid": gid,
                    "album_path": created.get("album_path") or "",
                    "wish_id": created.get("wish_id"),
                })
                queued.add(gid)
                artist_queued += 1
        out["summary"] = (
            f"{len(out['queued'])} album(s) queued from "
            f"{out['checked']} played artist(s)" if out["queued"]
            else "nothing new to queue from the artists you play")
        with _lock:
            _state.update({
                "last_run": time.time(),
                "last_result": out["summary"][:400],
                "queued": [q.get("title") for q in out["queued"]][:50],
            })
        _save_state()
        return out
    except Exception as e:
        traceback.print_exc()
        out["ok"] = False
        out["errors"].append(str(e))
        out["summary"] = f"cycle failed: {e}"
        with _lock:
            _state.update({"last_run": time.time(),
                           "last_result": out["summary"][:400]})
        _save_state()
        return out
    finally:
        with _lock:
            _running_cycle = False
        _set(running=False, current=[])


def _loop():
    """Run due cycles on a tick, until stopped."""
    persisted = _load_state()
    if persisted:
        with _lock:
            _state.update({
                "last_run": float(persisted.get("last_run") or 0),
                "last_result": str(persisted.get("last_result") or ""),
                "queued": list(persisted.get("queued") or []),
            })
    time.sleep(_SETTLE_S)
    while not _stop.is_set():
        try:
            run_cycle()
        except Exception:
            traceback.print_exc()
        for _ in range(max(1, _TICK_S // 5)):
            if _stop.is_set():
                return
            time.sleep(5.0)


def start():
    """Start the worker thread (idempotent)."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True,
                                   name="recommend-acquire")
        _thread.start()
        return _thread


def stop():
    """Stop the worker after the cycle in flight (if any)."""
    _stop.set()
