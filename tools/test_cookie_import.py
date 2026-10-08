#!/usr/bin/env python3
"""One cookie import for the cookie login: filtering, notes, per-cookie list.

Every claim below is about the real code, not a copy of it:

  * an import keeps only the cookies the credential is SENT to (a browser
    extension exports the whole profile) and reports how many cookie lines
    stayed out; a file with nothing for RYM is not stored — it comes back with
    `stored: 0` and a sentence saying which browser state to export instead, so
    the credential that was working survives it;
  * a per-cookie comment is keyed by the cookie's IDENTITY (domain, path,
    name), not by position: it survives a re-import that re-orders the file,
    and a comment on one cookie never shows against its neighbour;
  * the credential itself is untouched by a comment: RYM is still sent the
    exact header the import stored;
  * `GET/POST /api/cookies/{source}` are the panel's idiom (domain, path, name,
    expiry, expired, comment), they refuse an unknown source and a comment for
    a cookie the source does not hold, and no answer of theirs ever carries a
    cookie VALUE;
  * an expired export is named as expired — the failure that otherwise looks
    green: the jar parses, the panel is happy, and RYM answers as a guest.

Hermetic: the music folder is redirected to a temp dir (MLO_MUSIC_FOLDER) and
mlo.config/mlo.paths' CONFIG_FILE to a temp stub, so nothing of the real
install's app state, config.json or cookies is read, written or deleted. No
network is used.

Run:  python tools/test_cookie_import.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT = os.environ.pop("MLO_MUSIC_FOLDER", None)  # keep the test hermetic

tmp = tempfile.mkdtemp(prefix="mlo_cookieimport_")
music = os.path.join(tmp, "Music")
os.makedirs(music)
os.environ["MLO_MUSIC_FOLDER"] = music

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

stub_config = os.path.join(tmp, "config.json")
_real_config_files = {mod: mod.CONFIG_FILE for mod in (cfgmod, pathmod)}
for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = stub_config

from fastapi import HTTPException  # noqa: E402

from server import api_cookies, api_rym  # noqa: E402

HEADER = "# Netscape HTTP Cookie File\n"
RYM_HOST = "rateyourmusic.com"

# RYM's own export: the HttpOnly session cookie (the reason the import exists)
# next to the Cloudflare one, plus a site this credential is never sent to.
RYM_SESSION = "#HttpOnly_.rateyourmusic.com\tTRUE\t/\tTRUE\t1893456000\tsession\tRYM-VALUE\n"
RYM_CLEARANCE = ".rateyourmusic.com\tTRUE\t/\tTRUE\t1893456000\tcf_clearance\tCF-VALUE\n"
OTHER_SITE = ".example.com\tTRUE\t/\tFALSE\t1893456000\ttracking\tNOPE-VALUE\n"
RYM_PROFILE = HEADER + RYM_SESSION + RYM_CLEARANCE + OTHER_SITE

# The same two cookies in the OPPOSITE order: a re-export of the same session,
# which is what a positional comment would lose.
REORDERED = HEADER + RYM_CLEARANCE + RYM_SESSION

# A profile signed in to some OTHER site: a well-formed export worth nothing
# here, and it must not replace the credential that was working.
FOREIGN_ONLY = HEADER + OTHER_SITE

# A session cookie that expired years ago: the export that "just stopped
# working".
EXPIRED = HEADER + ".rateyourmusic.com\tTRUE\t/\tTRUE\t1000000000\tsession\tRYM-OLD\n" \
    + RYM_CLEARANCE

# The cookie VALUES in the fixtures: none may ever appear in an API answer.
SECRETS = ["RYM-VALUE", "CF-VALUE", "NOPE-VALUE", "RYM-OLD"]


def assert_no_values(payload, where):
    """The answer is columns, counts and sentences — never a cookie value."""
    blob = json.dumps(payload)
    for secret in SECRETS:
        assert secret not in blob, f"{where} leaked a cookie value: {blob}"


def cookie_data(text):
    """[(domain, name, value)] — the file's cookie data, as parsed."""
    from server.cookies import parse_cookies

    records, error = parse_cookies(text)
    assert error is None, error
    return [(c.domain, c.name, c.value) for c in records]


def post_rym(text):
    return api_rym.rym_cookies_post(api_rym.CookieUpload(text=text))


def comment(source, domain, path, name, text):
    return api_cookies.cookies_comment(
        source, api_cookies.CookieComment(domain=domain, path=path, name=name,
                                          comment=text))


def row(state, name, domain=RYM_HOST):
    """One cookie of a per-cookie answer, matched on BOTH domain and name."""
    for item in state["cookies"]:
        if item["name"] == name and item["domain"] == domain:
            return item
    raise AssertionError(f"{domain} {name} is not in {state}")


def refused(fn, status, want):
    """The HTTPException *fn* raises, with *want* in its sentence."""
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == status, (e.status_code, e.detail)
        assert want in e.detail, e.detail
        return e.detail
    raise AssertionError(f"nothing was refused (wanted {want!r})")


try:
    # ----------------------------------------------------------------- #
    # 1. The import keeps what the credential is sent, nothing else
    # ----------------------------------------------------------------- #
    # The shared parser reads the file the same way every Netscape reader does.
    assert cookie_data(RYM_PROFILE) == [
        ("rateyourmusic.com", "session", "RYM-VALUE"),
        ("rateyourmusic.com", "cf_clearance", "CF-VALUE"),
        ("example.com", "tracking", "NOPE-VALUE"),
    ], cookie_data(RYM_PROFILE)

    saved = post_rym(RYM_PROFILE)
    assert saved["stored"] == 2, saved
    assert saved["session"] is True, saved
    assert saved["filtered"] == 1, saved  # `.example.com`, and only that
    assert saved["names"] == ["session", "cf_clearance"], saved
    assert saved["warnings"] == [], saved
    assert_no_values(saved, "the RYM import's answer")
    assert cfgmod.load_config()["rym_cookie"] == \
        "session=RYM-VALUE; cf_clearance=CF-VALUE"
    assert pathmod.CONFIG_FILE == stub_config, pathmod.CONFIG_FILE

    # A file for a site this credential is NOT for is worth nothing, and the
    # credential that was working is left exactly as it was.
    before = cfgmod.load_config()["rym_cookie"]
    saved = post_rym(FOREIGN_ONLY)
    assert saved["stored"] == 0 and saved["filtered"] == 1, saved
    assert RYM_HOST in saved["warnings"][0], saved["warnings"]
    assert cfgmod.load_config()["rym_cookie"] == before, \
        "a foreign jar replaced RYM's cookie"

    # An export whose cookie has already expired says so: the failure that
    # otherwise looks exactly like a working credential.
    saved = post_rym(EXPIRED)
    assert any("expired" in w for w in saved["warnings"]), saved["warnings"]

    # Empty and non-cookie junk are refused in RYM's own terms.
    refused(lambda: post_rym(""), 400, "the file is empty")
    refused(lambda: post_rym("not a cookie file at all\n"), 400, "cookie")

    # ----------------------------------------------------------------- #
    # 2. One comment per cookie, keyed by identity, not by position
    # ----------------------------------------------------------------- #
    post_rym(RYM_PROFILE)
    state = comment("rym", RYM_HOST, "/", "session", "the signed-in one")
    assert row(state, "session")["comment"] == "the signed-in one", state
    assert row(state, "cf_clearance")["comment"] == "", state
    assert_no_values(state, "the comment's answer")
    # The credential is untouched by a comment: RYM is still sent the exact
    # header the import stored.
    assert cfgmod.load_config()["rym_cookie"] == \
        "session=RYM-VALUE; cf_clearance=CF-VALUE"

    # A RE-IMPORT of the same session in another order keeps the comment on the
    # cookie it was written for.
    saved = post_rym(REORDERED)
    assert saved["stored"] == 2, saved
    state = api_cookies.cookies_get("rym")
    assert row(state, "session")["comment"] == "the signed-in one", state
    assert row(state, "cf_clearance")["comment"] == "", state
    assert [c["name"] for c in state["cookies"]] == ["cf_clearance", "session"], state

    # Clearing the comment leaves the remembered expiry in place (it describes
    # the cookie, not the note).
    state = comment("rym", RYM_HOST, "/", "session", "")
    assert row(state, "session")["comment"] == "", state
    assert row(state, "session")["expiry"] == "1893456000", state
    assert row(state, "cf_clearance")["expiry"] == "1893456000", state
    notes = json.loads(cfgmod.load_config()["cookie_notes"])
    assert notes["rym"]["rateyourmusic.com\t/\tsession"] == {"expiry": "1893456000"}, notes
    assert notes["rym"]["rateyourmusic.com\t/\tcf_clearance"] == {"expiry": "1893456000"}, notes

    # The expiry column the file stated is per cookie, and the panel is told
    # whether it has passed — and, for a date it can name, when it dies.
    state = api_cookies.cookies_get("rym")
    assert row(state, "session")["expires_at"] == "2030-01-01", state
    assert row(state, "session")["expired"] is False, state
    assert row(state, "session")["path"] == "/", state
    assert_no_values(state, "the per-cookie list")

    # A comment that would break the store (a newline, or a small novel) is
    # flattened and capped rather than written into it.
    from server.cookies import MAX_COMMENT_CHARS

    state = comment("rym", RYM_HOST, "/", "session",
                    "line one\nline two" + "x" * 500)
    assert "\n" not in row(state, "session")["comment"], state
    assert len(row(state, "session")["comment"]) == MAX_COMMENT_CHARS, state
    # The re-import above stated the cookies in its own order, and that is the
    # header RYM is sent; a note never rewrites it.
    assert cfgmod.load_config()["rym_cookie"] == \
        "cf_clearance=CF-VALUE; session=RYM-VALUE", \
        "a long note changed the credential"

    # ----------------------------------------------------------------- #
    # 3. The route refuses what it cannot attach a note to
    # ----------------------------------------------------------------- #
    refused(lambda: api_cookies.cookies_get("bandcamp"), 404,
            "unknown cookie source")
    refused(lambda: comment("rym", RYM_HOST, "/", "nope", "x"),
            404, "no nope cookie")
    refused(lambda: comment("rym", RYM_HOST, "/", "", "x"),
            404, "no unnamed cookie")

    print("PASS  cookie import: filtering, per-cookie comments, identity keys, "
          "RYM credential")
finally:
    if OUT:
        os.environ["MLO_MUSIC_FOLDER"] = OUT
    else:
        os.environ.pop("MLO_MUSIC_FOLDER", None)
    for _mod, _path in _real_config_files.items():
        _mod.CONFIG_FILE = _path
    cfgmod._MIGRATED = False
    shutil.rmtree(tmp, ignore_errors=True)