#!/usr/bin/env python3
"""Route smoke test: hits every FastAPI route with real library values.

Usage:  python tools/smoke_api.py [base_url]
Exits non-zero when any GET route answers >=400.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")


def req(path, method="GET", body=None, timeout=60):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else (b"" if method != "GET" else None)
    r = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001 - smoke test reports, never raises
        return 0, str(e).encode()


def main():
    status, raw = req("/api/library")
    if status != 200:
        print(f"library fetch failed: {status} {raw[:200]}")
        return 2
    lib = json.loads(raw)
    albums = [a for ar in lib.get("artists", []) for a in ar.get("albums", [])]
    album = next((a for a in albums if a.get("tracks")), albums[0] if albums else {})
    artist = next((ar for ar in lib.get("artists", []) if ar.get("albums")), {})
    apath = album.get("path") or ""
    tpath = os.path.join(apath, (album.get("tracks") or [{}])[0].get("file", ""))
    arpath = artist.get("path") or ""
    artist_name = artist.get("name") or ""
    track_title = os.path.splitext(os.path.basename(tpath))[0]
    q = urllib.parse.quote

    cases = [
        ("/api/health", "GET", None),
        ("/api/config", "GET", None),
        ("/api/config/defaults", "GET", None),
        ("/api/library", "GET", None),
        ("/api/home", "GET", None),
        ("/api/favorites", "GET", None),
        ("/api/likes", "GET", None),
        ("/api/playlists", "GET", None),
        ("/api/dependencies", "GET", None),
        ("/api/export/codecs", "GET", None),
        ("/api/export/drives", "GET", None),
        ("/api/wishes", "GET", None),
        ("/api/soulseek/status", "GET", None),
        ("/api/soulseek/auto", "GET", None),
        ("/api/soulseek/shares", "GET", None),
        ("/api/soulseek/uploads", "GET", None),
        ("/api/soulseek/downloads", "GET", None),
        ("/api/soulseek/review", "GET", None),
        ("/api/beets/status", "GET", None),
        ("/api/videos/scan", "GET", None),
        ("/api/fs/list", "GET", None),
        (f"/api/album?path={q(apath)}", "GET", None),
        (f"/api/artist?path={q(arpath)}", "GET", None),
        (f"/api/tags?path={q(tpath)}", "GET", None),
        (f"/api/replaygain?path={q(tpath)}", "GET", None),
        (f"/api/videos/meta?path={q(tpath)}", "GET", None),
        (f"/api/videos/subtitles?path={q(tpath)}", "GET", None),
        (f"/api/cover?album={q(apath)}", "GET", None),
        (f"/api/cover/info?album={q(apath)}", "GET", None),
        (f"/api/album/mbdetect?path={q(apath)}", "GET", None),
        (f"/api/album/scan-tracks?path={q(apath)}", "GET", None),
        (f"/api/lyrics/get?artist={q(artist_name)}&track={q(track_title)}", "GET", None),
        (f"/api/lyrics/search?artist={q(artist_name)}&track={q(track_title)}", "GET", None),
        ("/api/mb/search?q=test", "GET", None),
        ("/api/mb/search/artists?q=test", "GET", None),
        ("/api/mb/search/releases?q=test", "GET", None),
        ("/api/rym/validate?url=https://rateyourmusic.com/release/album/x/y", "GET", None),
        ("/api/stream?path=" + q(tpath), "GET", None),
        ("/api/videos/stream?path=" + q(tpath), "GET", None),
        ("/api/track/download?path=" + q(tpath), "GET", None),
        ("/api/track/export?path=" + q(tpath), "GET", None),
        ("/api/naming/preview", "POST", {"path": tpath}),
        ("/api/organize", "POST", {"paths": [apath], "dry_run": True}),
        ("/api/run", "POST", {"ids": [], "targets": []}),
        ("/api/playlists", "POST", {"name": "smoke-tmp", "kind": "manual"}),
    ]
    bad = 0
    for path, method, body in cases:
        st, raw = req(path, method, body)
        # /api/lyrics/* proxies LRCLIB: a 404 for a synthetic test track just
        # means the service has no such lyrics, not that the route is broken.
        allowed = st == 404 and path.startswith("/api/lyrics")
        ok = (200 <= st < 400) or allowed
        if not ok:
            bad += 1
        show = "" if ok else "  <<< " + raw[:160].decode("utf-8", "replace").replace("\n", " ")
        print(f"{st:>4} {method:<5} {path[:88]}{show}")
    # Clean up after ourselves (and exercise the DELETE route).
    st, raw = req("/api/playlists")
    if st == 200:
        for pl in json.loads(raw):
            if pl.get("name") == "smoke-tmp":
                dst, _ = req(f"/api/playlists/{pl['id']}", "DELETE")
                print(f"{dst:>4} DEL   /api/playlists/{pl['id']}  (cleanup)")
                if not 200 <= dst < 400:
                    bad += 1

    print(f"\n{len(cases) - bad}/{len(cases)} ok, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
