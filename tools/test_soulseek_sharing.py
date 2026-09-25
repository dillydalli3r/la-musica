#!/usr/bin/env python3
"""Audit the Soulseek share: what is generated, what is reported, what is said.

Sharing fails silently in slskd in several ways that all look like success: a
share path or filter it cannot validate stops the daemon from booting at all
(no daemon, no share, no search), an unreadable subfolder is skipped without a
word, a share that was never scanned indexes nothing, and a scan that finished
with an empty index is still a completed scan. These asserts pin the config this
app generates (which must not be able to hit any of that), the audit that names
each failure mode, and the errors that have to reach the caller.

Offline: slskd's REST API is a stub installed on the module's HTTP client, no
socket is opened, and the music folder, the generated config and the daemon's
log all live in a temp directory.

Run:  python tools/test_soulseek_sharing.py
"""
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from server import soulseek

_TMP = tempfile.mkdtemp(prefix="mlo_slskd_share_")
MUSIC = os.path.join(_TMP, "Music")
# the library root — what `soulseek_share_dirs` defaults to
ARTISTS = os.path.join(MUSIC, "Artists")
ALBUM = os.path.join(ARTISTS, "Some Artist", "Some Album (1999)")
os.makedirs(ALBUM)
TRACK = os.path.join(ALBUM, "01 - A Track.flac")
TRACK_SIZE = 4100
with open(TRACK, "wb") as f:
    f.write(b"fLaC" + b"\0" * (TRACK_SIZE - 4))
# the app's own state tree, the staging folder a download lands in, and the junk
# a library keeps after a trip through a NAS and a Mac
os.makedirs(os.path.join(MUSIC, ".mlo", "downloads", "Some User"))
with open(os.path.join(MUSIC, ".mlo", "downloads", "Some User", "partial.flac"), "wb") as f:
    f.write(b"partial")
os.makedirs(os.path.join(MUSIC, "@eaDir"))
with open(os.path.join(MUSIC, "@eaDir", "thumb.jpg"), "wb") as f:
    f.write(b"junk")
with open(os.path.join(MUSIC, "Thumbs.db"), "wb") as f:
    f.write(b"junk")
# two folders with the SAME leaf name: slskd refuses to start on their colliding
# aliases, so the generated config has to tell them apart
SAME_A = os.path.join(_TMP, "a", "Music")
SAME_B = os.path.join(_TMP, "b", "Music")
os.makedirs(SAME_A)
os.makedirs(SAME_B)

CFG = {
    "music_folder": MUSIC,
    "soulseek_username": "tester",
    "soulseek_password": "secret",
    "soulseek_share_library": True,
}

# the daemon's log is read from beside the generated config, which lives in the
# install's data folder — keep both in the temp tree, never the real one
LOG = os.path.join(_TMP, "slskd.log")
soulseek.config_path = lambda: os.path.join(_TMP, "slskd.yaml")
with open(LOG, "w", encoding="utf-8") as f:
    f.write("[00:00:01 INF] Starting shared file scan\n"
            "[00:00:09 INF] Scan found 1 files (and 0 were filtered) in 8100ms\n")


class _Stream:
    """`with client.stream(...)` as the browse probe uses it."""

    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, *_exc):
        return False


class FakeSlskd:
    """slskd 0.26's share-related REST surface, canned.

    Shapes are the daemon's own: GET /application carries the scan state under
    `shares` (GET /shares carries none), GET /shares maps host -> share entries,
    GET /options returns what the RUNNING daemon was started with, and
    GET /shares/contents is the browse index it answers with."""

    def __init__(self):
        self.error = None
        self.statuses = {}
        self.calls = []
        self.scan = {"scanning": False, "scanPending": False, "ready": False,
                     "faulted": False, "cancelled": False, "scanProgress": 0.0,
                     "directories": 0, "files": 0, "hosts": ["local"]}
        self.shares = {"local": [{"id": "a", "alias": "Artists", "isExcluded": False,
                                  "localPath": ARTISTS, "remotePath": "Artists",
                                  "directories": 3, "files": 1}]}
        self.options = {
            "shares": {"directories": [ARTISTS],
                       "filters": [x.strip("'") for x in soulseek.share_exclude(CFG)]},
            "flags": {},
        }
        self.contents = self._contents_for(TRACK, TRACK_SIZE)
        self.logged_in = True
        # slskd's upload tree (what others downloaded from us) — empty until a
        # test seeds a transfer, which is also the app's only proof that peers
        # reached the listen port from outside.
        self.uploads = []

    @staticmethod
    def _contents_for(path, size):
        rel = os.path.relpath(path, ARTISTS).replace(os.sep, "\\")
        directory, _, name = rel.rpartition("\\")
        return [{"name": "Artists\\" + directory,
                 "files": [{"filename": name, "size": size, "extension": "flac"}]}]

    def _payload(self, path):
        if path == "/application":
            return {"user": {"username": "tester"}, "shares": dict(self.scan)}
        if path == "/shares":
            return self.shares
        if path == "/options":
            return self.options
        if path == "/shares/contents":
            return self.contents
        if path == "/server":
            return {"isLoggedIn": self.logged_in}
        if path == "/transfers/uploads":
            return self.uploads
        return None

    def request(self, method, path, json=None, headers=None, timeout=None):
        self.calls.append((method, path))
        req = httpx.Request(method, "http://slskd" + path)
        if self.error is not None:
            raise self.error
        status = self.statuses.get(f"{method} {path}", 200)
        if status >= 300:
            text = ("A share scan is already in progress."
                    if status == 409 else "slskd is unhappy")
            return httpx.Response(status, request=req,
                                  headers={"content-type": "text/plain"},
                                  text=text)
        payload = self._payload(path) if method == "GET" else None
        if payload is None:
            return httpx.Response(status, request=req)
        return httpx.Response(status, request=req, json=payload)

    def get(self, path, headers=None, timeout=None):
        return self.request("GET", path, headers=headers, timeout=timeout)

    def stream(self, method, path, headers=None, timeout=None):
        return _Stream(self.request(method, path, headers=headers, timeout=timeout))


fake = FakeSlskd()
soulseek._http_client = lambda *a, **k: fake


def shares_of(text):
    """The generated YAML's shares section: (directories, filters), verbatim."""
    lines = text.splitlines()
    i = lines.index("shares:")
    assert lines[i + 1] == "  directories:", lines[i + 1]
    dirs, filters, mode = [], [], None
    for line in lines[i + 1:]:
        if line == "  directories:":
            mode = "d"
        elif line == "  filters:":
            mode = "f"
        elif line.startswith("    - ") and mode:
            (dirs if mode == "d" else filters).append(line[6:])
        else:
            break
    return dirs, filters


def slskd_share(entry):
    """One `shares.directories` entry as SLSKD parses it: (alias, path).

    Share(string) splits `[alias]path` — including the '-'/'!' prefix that marks
    an exclusion — and falls back to the last path segment as the alias, which
    is exactly what its own validation rejects as a collision."""
    value = entry.strip('"').replace("\\\\", "\\").strip("-!")
    m = re.match(r"^\[([^\]]*)\](.*)$", value)
    if m:
        return m.group(1), m.group(2)
    return value.rstrip("\\/").split("\\")[-1].split("/")[-1], value


# --------------------------------------------------------------------------- #
# 1) the generated config is share-correct by construction
# --------------------------------------------------------------------------- #
text, _key = soulseek.generate_yaml(CFG)
dirs, filters = shares_of(text)
# the DEFAULT share is the library root, not the music folder it lives in: the
# music folder also holds .mlo (data, downloads, trash) and anything not filed
# by the organizer yet, none of which belongs on the network
assert soulseek.share_dirs(CFG) == [ARTISTS], soulseek.share_dirs(CFG)
assert [slskd_share(d) for d in dirs] == [("Artists", ARTISTS)], dirs
# an explicit list still wins, and a music folder that is not set shares nothing
assert soulseek.share_dirs(dict(CFG, soulseek_share_dirs=[MUSIC])) == [MUSIC]
assert soulseek.share_dirs(dict(CFG, music_folder="", soulseek_share_dirs=[])) == []
explicit, _f = shares_of(soulseek.generate_yaml(dict(CFG, soulseek_share_dirs=[MUSIC]))[0])
assert [slskd_share(d) for d in explicit] == [("Music", MUSIC)], explicit

# every filter is a real regex, and the app's own state tree is filtered out
assert soulseek._RESERVED_SHARE_FILTERS[0] in filters, filters
compiled = [re.compile(x.strip("'")) for x in filters]
under_mlo = os.path.join(MUSIC, ".mlo", "data", "slskd.yaml")
assert any(r.search(under_mlo) for r in compiled), "the .mlo state folder is shared"

# ...but the library itself, its album folders and a file whose NAME merely
# contains the word are not filtered
assert not any(r.search(TRACK) for r in compiled), "the library is filtered out"
assert not any(r.search(MUSIC) for r in compiled), "the share root is filtered out"
assert not any(r.search(os.path.join(MUSIC, "Artists")) for r in compiled)

# NAS/Mac/Explorer junk never reaches the network either
assert any(r.search(os.path.join(MUSIC, "@eaDir", "thumb.jpg")) for r in compiled), \
    "@eaDir is shared"
assert any(r.search(os.path.join(MUSIC, "Thumbs.db")) for r in compiled)
assert any(r.search(os.path.join(MUSIC, "Artists", "._01.flac")) for r in compiled)

# the download dir is never a second share, and sharing can be turned off
downloads = soulseek.download_dir(CFG)
assert downloads not in [slskd_share(d)[1] for d in dirs], dirs
(text_off, _k) = soulseek.generate_yaml(dict(CFG, soulseek_share_library=False))
assert "\nshares:" not in text_off, "sharing is off but the config still shares"

# slskd validates transfers.upload.slots as Range(1, int.MaxValue) and EXITS
# when it is out of range — the settings field calls 0 "unlimited", so a 0 used
# to leave every install with no daemon at all (and therefore no share)
for value, want in ((0, 10), (None, 10), ("", 10), ("4", 4), (20, 20), (-3, 1)):
    cfg = dict(CFG)
    if value is None:
        cfg.pop("soulseek_upload_slots", None)
    else:
        cfg["soulseek_upload_slots"] = value
    body, _k = soulseek.generate_yaml(cfg)
    slots = int([l for l in body.splitlines() if l.strip().startswith("slots:")][0].split(":")[1])
    assert slots == want, (value, slots)
    assert slots >= 1, "slskd refuses to boot on slots < 1"

# ...and the same for the download slots, which the app clamps to 1..20
for value, want in ((0, 1), (None, 9), ("7", 7), (99, 20)):
    cfg = dict(CFG)
    if value is None:
        cfg.pop("soulseek_download_slots", None)
    else:
        cfg["soulseek_download_slots"] = value
    body, _k = soulseek.generate_yaml(cfg)
    slots = [int(l.split(":")[1]) for l in body.splitlines()
             if l.strip().startswith("slots:")][1]
    assert slots == want, (value, slots)

# two folders with the same leaf name are aliased apart: slskd rejects the
# collision by refusing to start, which takes the share down completely
both = dict(CFG, soulseek_share_dirs=[SAME_A, SAME_B])
dirs2, _filters2 = shares_of(soulseek.generate_yaml(both)[0])
parsed = [slskd_share(d) for d in dirs2]
assert len(parsed) == 2, dirs2
assert {a for a, _p in parsed} == {"Music", "Music-2"}, parsed
assert {p for _a, p in parsed} == {SAME_A, SAME_B}, parsed

# the same folder twice (the settings field is free text) and a relative path
# are left out instead of written
typos = dict(CFG, soulseek_share_dirs=[
    MUSIC, MUSIC.replace(os.sep, "/") if os.sep != "/" else MUSIC + os.sep,
    "relative/music",
])
dirs3, _f3 = shares_of(soulseek.generate_yaml(typos)[0])
assert [slskd_share(d) for d in dirs3] == [("Music", MUSIC)], dirs3

# a filter slskd cannot compile would stop the daemon from booting: it is not
# written, and the reserved ones still are
bad = dict(CFG, soulseek_share_exclude=["[unclosed", r"\.tmp$"])
_d4, filters4 = shares_of(soulseek.generate_yaml(bad)[0])
assert "'[unclosed'" not in filters4, filters4
assert any(x.strip("'") == r"\.tmp$" for x in filters4), filters4
for x in filters4:
    re.compile(x.strip("'"))

print("ok  generated slskd config shares the library root, filters .mlo/@eaDir "
      "and can never hold an entry or a pattern slskd refuses to boot with")


# --------------------------------------------------------------------------- #
# 2) the audit names each failure mode
# --------------------------------------------------------------------------- #
# This machine's own listen-port probe (`soulseek.listen_port_state`) is two
# real socket tests on a REAL port, and a test machine has nothing accepting on
# it — which the port check's own rule (server/soulseek_port._listen_check)
# calls a fail, and the audit now reports as one too (see the dead-port case
# below). Every fixture here is about the SHARE, so the listener is stubbed as
# the one slskd would be running; the router half of the payload stays real.
_REAL_LISTEN_STATE = soulseek.listen_port_state


def _listening_port(cfg=None):
    """A payload whose listen row is ok: something accepts on the port."""
    return dict(_REAL_LISTEN_STATE(cfg), listening=True, holder="slskd",
                bindable=False, conflict=None, error="")


soulseek.listen_port_state = _listening_port


def ready(**over):
    """A healthy slskd: scan complete, the library indexed and served."""
    fake.error = None
    fake.statuses.clear()
    fake.logged_in = True
    fake.scan = {"scanning": False, "scanPending": False, "ready": True,
                 "faulted": False, "cancelled": False, "scanProgress": 1.0,
                 "directories": 3, "files": 1, "hosts": ["local"]}
    fake.shares = {"local": [{"id": "a", "alias": "Artists", "isExcluded": False,
                              "localPath": ARTISTS, "remotePath": "Artists",
                              "directories": 3, "files": 1}]}
    fake.options = {
        "shares": {"directories": [ARTISTS],
                   "filters": [x.strip("'") for x in soulseek.share_exclude(CFG)]},
        "flags": {},
    }
    fake.contents = FakeSlskd._contents_for(TRACK, TRACK_SIZE)
    fake.scan.update(over)
    return fake


# a share that really is up: the one state that must NOT be reported as a problem
ready()
audit = soulseek.share_audit(CFG, probe=True)
assert audit["status"] == "ok", (audit["status"], audit["problems"])
assert audit["ok"] is True
assert audit["scan"]["files"] == 1 and audit["scan"]["state"] == "complete"
assert audit["disk"]["audio_files"] == 1, audit["disk"]
assert audit["browse"]["ok"] is True, audit["browse"]
assert "01 - A Track.flac" in audit["browse"]["detail"], audit["browse"]
assert "Starting shared file scan" in audit["scan"]["log"][0], audit["scan"]["log"]

# ...and the mapping the app ASKED for and did not get: the share is served and
# searchable, but a peer reaches it by connecting BACK to the listen port, so a
# green "other users can browse and download" here is the one lie the card must
# not tell (the owner's report: the card said shared, their client could not
# browse). A router that granted a mapping keeps it green — that is the state
# below this one.
ready()
saved_map = soulseek._PORTMAP.get("result")
soulseek._PORTMAP["result"] = {
    "state": "no_gateway",
    "detail": "UPnP: no device answered the UPnP search on 239.255.255.250:1900.",
}
audit = soulseek.share_audit(CFG, probe=True)
assert audit["status"] == "listen_unconfirmed", (audit["status"], audit["problems"])
assert audit["ok"] is False
codes = [p["code"] for p in audit["problems"]]
assert "listen_unreachable" in codes, codes
lic = next(p for p in audit["problems"] if p["code"] == "listen_unreachable")
assert "50000" in lic["message"], lic
assert "Forward TCP 50000" in lic["hint"], lic
assert "can find and search them" in audit["summary"], audit["summary"]
assert "no forward was confirmed" in audit["summary"], audit["summary"]
assert audit["browse"]["ok"] is True, "the index is still served, and is still probed"

# in a container the remedy has to be about the HOST: the gateway a container
# can see is Docker's bridge, so the forward the app asks for by itself can
# never be made, and the router cannot forward to a container address
import server.auth as srv_auth
from server import soulseek_port

real_in_container = srv_auth.in_container
real_connect = soulseek_port._connect
srv_auth.in_container = lambda: True
# The suite stays offline: the publish row's connects are answered from a table
# (what a real published port looks like on a gateway is proven in
# tools/test_soulseek_port.py, against real sockets).
published = {"accepts": set()}


def _gateway(host, port, timeout):
    return (port in published["accepts"],
            "" if port in published["accepts"] else "connection refused")


soulseek_port._connect = _gateway

audit = soulseek.share_audit(CFG)
hint = next(p for p in audit["problems"] if p["code"] == "listen_unreachable")["hint"]
assert "Running in a container" in hint and "HOST's LAN address" in hint, hint
assert "50000:50000" in hint, hint
# ...and where the host's own port list could not be read (the control refused
# too), the hint says that instead of pretending to know
assert "docker port <container-name>" in hint, hint

# The half of the remedy the numbers cannot pick: R279 keeps the compose file and
# the daemon from disagreeing about the NUMBER, and the publish line is whether
# that number is published to the container at all. The app measures it from in
# here — the gateway hands back the app's OWN published port (8000) but not the
# listen port, so the compose file is what is wrong and the router is downstream
# of it. Before this, the audit told the owner to fix both.
published["accepts"] = {8000}
audit = soulseek.share_audit(CFG)
codes = [p["code"] for p in audit["problems"]]
assert "listen_unpublished" in codes, codes
unpub = next(p for p in audit["problems"] if p["code"] == "listen_unpublished")
assert "does not publish TCP 50000" in unpub["message"], unpub
assert "ports: \"50000:50000\"" in unpub["hint"], unpub
assert "MLO_SOULSEEK_LISTEN_PORT" in unpub["hint"], unpub
assert "the container's host does not publish the listen port" in audit["summary"], \
    audit["summary"]
assert audit["status"] == "listen_unconfirmed" and audit["ok"] is False, audit["status"]
# the port readout names every port that has to be reachable, which is one
assert any("TCP 50000 is the only port involved" in n for n in audit["notes"]), \
    audit["notes"]

# ...and when the host DOES publish it, the compose file is cleared by
# measurement and what is left is in front of the host — named as the router, at
# the address the internet sees (a tunnel or a second NAT changes that address)
published["accepts"] = {50000}
audit = soulseek.share_audit(CFG)
codes = [p["code"] for p in audit["problems"]]
assert "listen_unpublished" not in codes, codes
hint = next(p for p in audit["problems"] if p["code"] == "listen_unreachable")["hint"]
assert "the host publishes TCP 50000" in hint, hint
assert "the compose line is not the problem" in hint, hint
assert "address the internet actually sees" in hint, hint
assert audit["status"] == "listen_unconfirmed", audit["status"]

soulseek_port._connect = real_connect
published["accepts"] = set()
srv_auth.in_container = real_in_container

# ...and the state CLEARS once peers have actually reached the port: a served
# upload is a connection THEY opened, which is stronger evidence than a missing
# mapping (the by-hand forward a container can never read back)
ready()
fake.uploads = [{"username": "someone", "directories": [
    {"files": [{"filename": "01 - A Track.flac", "state": "Completed, Succeeded"}]}]}]
audit = soulseek.share_audit(CFG)
assert audit["status"] == "ok", (audit["status"], audit["problems"])
assert audit["ok"] is True
assert any("1 transfer(s) have been served" in n for n in audit["notes"]), audit["notes"]
fake.uploads = []

# a mapping the router confirmed keeps the audit green: this state is about the
# app's own request not being answered, not about every install without UPnP
ready()
soulseek._PORTMAP["result"] = {"state": "mapped", "verified": True,
                               "internal_ip": "192.168.1.20",
                               "detail": "UPnP: the router lists the mapping."}
audit = soulseek.share_audit(CFG)
assert audit["status"] == "ok", (audit["status"], audit["problems"])
assert audit["ok"] is True
soulseek._PORTMAP["result"] = saved_map

# a DEAD listen port is not "ok" either: with nothing accepting on the port (or
# another program holding it) slskd still serves the index, so the card used to
# answer status "ok" and "other users can ... download them" while
# /api/soulseek/port-check's OWN listen row (soulseek_port._listen_check — the
# same port_status_payload measurement) said fail. The audit's verdict is that
# row's, so the two can never disagree.
from server import soulseek_port

ready()
soulseek.listen_port_state = lambda cfg=None: dict(
    _REAL_LISTEN_STATE(cfg), listening=False, holder="", bindable=True,
    conflict=None,
    error="slskd is running but nothing is listening on the Soulseek listen "
          "port 50000")
audit = soulseek.share_audit(CFG, probe=True)
assert audit["status"] == "listen_unconfirmed", (audit["status"], audit["problems"])
assert audit["ok"] is False
assert soulseek_port._listen_check(audit["port"])["state"] == "fail", \
    "the audit must agree with the port check's own listen row"
codes = [p["code"] for p in audit["problems"]]
assert "listen_unreachable" in codes, codes
assert "nothing accepts a connection on the listen port" in audit["summary"], \
    audit["summary"]
assert "download them" not in audit["summary"], audit["summary"]

# ...and another program holding the port is the same verdict, in the port's own
# words (slskd cannot listen on a port somebody else holds, so a peer reaches
# that program and nothing of this share)
soulseek.listen_port_state = lambda cfg=None: dict(
    _REAL_LISTEN_STATE(cfg), listening=True, holder="another program",
    bindable=False,
    conflict="another program is listening on the Soulseek listen port 50000 "
             "— slskd cannot use it while that program runs")
audit = soulseek.share_audit(CFG)
assert audit["status"] == "listen_unconfirmed", (audit["status"], audit["problems"])
assert audit["ok"] is False
assert "another program is listening on the listen port" in audit["summary"], \
    audit["summary"]
soulseek.listen_port_state = _listening_port

# never scanned: the index is empty and the daemon is not scanning it either
ready(ready=False, files=0, directories=0, scanProgress=0.0)
audit = soulseek.share_audit(CFG)
assert audit["status"] == "not_scanned", audit
assert audit["ok"] is False
assert "problems" in audit and audit["problems"][0]["code"] == "no_scan_yet"
assert "indexing the shared folders yet" in audit["summary"], audit["summary"]

# scanning right now: progress is reported, not a verdict
ready(ready=False, files=0, scanning=True, scanProgress=0.4217)
audit = soulseek.share_audit(CFG)
assert audit["status"] == "scanning", audit["status"]
assert audit["scan"]["progress"] == 42.2, audit["scan"]["progress"]
assert "42.2% done" in audit["summary"], audit["summary"]

# a failed scan: slskd's own last word about scanning is carried with it
ready(ready=False, files=0, faulted=True)
with open(LOG, "a", encoding="utf-8") as f:
    f.write("[00:01:00 ERR] Encountered error during scan of shared files: "
            "Access to the path 'F:\\Music\\x' is denied\n")
audit = soulseek.share_audit(CFG)
assert audit["status"] == "scan_failed", audit["status"]
assert audit["scan"]["state"] == "failed"
assert "is denied" in audit["problems"][0]["message"], audit["problems"][0]

# a scan that finished with an empty index while the folder holds audio: the
# classic "sharing is on, nobody can see anything"
ready(files=0, directories=0)
audit = soulseek.share_audit(CFG)
assert audit["status"] == "empty_share", audit["status"]
assert audit["ok"] is False
assert "0 files" in audit["summary"] and "1 audio file sits" in audit["summary"], audit["summary"]
assert audit["problems"][0]["code"] == "index_empty"

# the browse endpoint does not answer: reported as unreadable, with slskd's words
ready()
fake.statuses["GET /shares/contents"] = 500
audit = soulseek.share_audit(CFG, probe=True)
assert audit["status"] == "unbrowsable", (audit["status"], audit["problems"])
assert audit["browse"]["ok"] is False
assert "slskd is unhappy" in audit["browse"]["detail"], audit["browse"]
assert audit["problems"][0]["code"] == "browse_unreachable"
assert "slskd is unhappy" in audit["problems"][0]["message"], audit["problems"][0]

# ...or it answers without a file that is on disk (a filter or a permission the
# scan skipped silently)
ready()
fake.contents = [{"name": "Music\\Artists\\Some Other Album",
                  "files": [{"filename": "01 - Other.flac", "size": 12,
                             "extension": "flac"}]}]
audit = soulseek.share_audit(CFG, probe=True)
assert audit["status"] == "unbrowsable", audit["status"]
assert audit["browse"]["ok"] is False
assert "not in the index" in audit["browse"]["detail"], audit["browse"]

# a size that does not match the disk is not the same file
ready()
fake.contents = FakeSlskd._contents_for(TRACK, TRACK_SIZE + 1)
audit = soulseek.share_audit(CFG, probe=True)
assert audit["browse"]["ok"] is False, audit["browse"]
assert "size differs" in audit["browse"]["detail"], audit["browse"]

# the probe stops reading a share index bigger than its cap instead of guessing
ready()
dirs, truncated, error = soulseek._share_contents(CFG, limit_bytes=1)
assert truncated is True and error == "" and dirs == [], (dirs, truncated, error)

# a shared folder that is not there (or was never readable) is its own state
missing = dict(CFG, soulseek_share_dirs=[os.path.join(_TMP, "gone")])
audit = soulseek.share_audit(missing)
assert audit["status"] == "path_unreadable", audit["status"]
assert audit["problems"][0]["code"] == "share_missing"
assert "gone" in audit["problems"][0]["message"]

# slskd is running with a share list this app did not write (a stale config):
# the daemon has to be restarted before anyone can browse the library
ready()
fake.shares = {"local": []}
audit = soulseek.share_audit(CFG)
assert audit["status"] == "config_mismatch", audit["status"]
assert audit["shares"]["mismatch"] is True
assert audit["problems"][0]["code"] == "share_not_live"

# ...or with the folder excluded with slskd's '-' prefix
ready()
fake.shares = {"local": [{"id": "a", "alias": "Artists", "isExcluded": True,
                          "localPath": ARTISTS, "remotePath": "Artists",
                          "directories": 0, "files": 0}]}
audit = soulseek.share_audit(CFG)
assert audit["status"] == "config_mismatch", audit["status"]
assert audit["problems"][0]["code"] == "share_excluded"

# ...or with a filter set that differs from the generated one
ready()
fake.options = {"shares": {"directories": [ARTISTS], "filters": ["something_else"]},
                "flags": {}}
audit = soulseek.share_audit(CFG)
assert audit["status"] == "config_mismatch", audit["status"]
assert audit["filters"]["mismatch"] is True
assert audit["problems"][0]["code"] == "filters_stale"

# slskd started with no_share_scan skips the scan that publishes the library
ready(ready=False, files=0)
fake.options = {"shares": {"directories": [ARTISTS],
                           "filters": [x.strip("'") for x in soulseek.share_exclude(CFG)]},
                "flags": {"no_share_scan": True}}
audit = soulseek.share_audit(CFG)
assert audit["status"] == "not_scanned", audit["status"]
assert any(p["code"] == "scan_disabled" for p in audit["problems"]), audit["problems"]

# not logged in: the scan is fine and the library is still invisible
ready()
fake.logged_in = False
audit = soulseek.share_audit(CFG)
assert audit["status"] == "not_logged_in", audit["status"]
assert audit["problems"][0]["code"] == "logged_out"

# ...or the daemon was told never to connect at all (a flag this app never
# writes, so it can only come from a foreign config)
ready()
fake.options = {"shares": {"directories": [ARTISTS],
                           "filters": [x.strip("'") for x in soulseek.share_exclude(CFG)]},
                "flags": {"no_connect": True}}
audit = soulseek.share_audit(CFG)
assert audit["status"] == "not_logged_in", audit["status"]
assert any(p["code"] == "connection_disabled" for p in audit["problems"]), audit["problems"]
assert "no_connect" in audit["problems"][0]["message"], audit["problems"][0]

# the daemon is not answering at all
ready()
fake.error = httpx.ConnectError("connection refused")
audit = soulseek.share_audit(CFG)
assert audit["status"] == "not_running", audit["status"]
assert audit["running"] is False
assert audit["problems"][0]["code"] == "slskd_down"

# sharing turned off on purpose is a state, not a failure — but only once the
# daemon has stopped serving the old list (it reads its shares at boot)
ready()
off = dict(CFG, soulseek_share_library=False)
audit = soulseek.share_audit(off)
assert audit["status"] == "config_mismatch", audit["status"]
assert audit["ok"] is False
assert "still serves 1 shared folder(s)" in audit["summary"], audit["summary"]
assert audit["problems"][0]["code"] == "still_sharing"

fake.error = httpx.ConnectError("connection refused")
audit = soulseek.share_audit(off)
assert audit["status"] == "disabled" and audit["ok"] is True, audit["status"]
assert audit["shares"]["configured"], "the configured folders are still reported"
fake.error = None

# nothing configured at all
audit = soulseek.share_audit(dict(CFG, music_folder="", soulseek_share_dirs=[]))
assert audit["status"] == "unconfigured", audit["status"]
assert audit["problems"][0]["code"] == "no_share_dir"

# a config slskd would refuse (a bad path and a bad pattern) is reported, and
# the daemon that IS running still shares — it serves the explicit folder the
# config asks for (the default would be the library root)
ready()
fake.shares = {"local": [{"id": "a", "alias": "Music", "isExcluded": False,
                          "localPath": MUSIC, "remotePath": "Music",
                          "directories": 3, "files": 1}]}
fake.options = {"shares": {"directories": [MUSIC],
                           "filters": [x.strip("'") for x in soulseek.share_exclude(CFG)]},
                "flags": {}}
audit = soulseek.share_audit(dict(CFG, soulseek_share_dirs=[MUSIC, "relative/music"],
                                  soulseek_share_exclude=["[unclosed"]))
assert audit["status"] == "misconfigured", (audit["status"], audit["problems"])
codes = {p["code"] for p in audit["problems"]}
assert codes == {"share_dropped", "filter_invalid"}, codes
assert audit["filters"]["invalid"][0]["pattern"] == "[unclosed"

print("ok  the share audit tells no-scan-yet, scan-running, scan-failed, "
      "empty-index, unbrowsable, unconfirmed-listen-port, missing-folder, "
      "stale-config, logged-out, daemon-down, sharing-off and misconfigured apart")


# --------------------------------------------------------------------------- #
# 3) slskd's own refusals reach the caller
# --------------------------------------------------------------------------- #
ready()
fake.statuses["PUT /shares"] = 409
try:
    soulseek.rescan_shares(CFG)
except soulseek.SlskdError as e:
    assert "already scanning" in str(e), str(e)
else:
    raise AssertionError("a refused rescan reported success")

fake.statuses["PUT /shares"] = 500
try:
    soulseek.rescan_shares(CFG)
except soulseek.SlskdHTTPError as e:
    assert "slskd is unhappy" in str(e), str(e)
    assert e.response.status_code == 500
else:
    raise AssertionError("a failed rescan was swallowed")

# the endpoint the UI presses turns both into an answer, not a bare 500
import server.main as srv_main

ready()
fake.statuses["PUT /shares"] = 409
try:
    srv_main.soulseek_shares_rescan()
except Exception as e:  # fastapi.HTTPException
    assert getattr(e, "status_code", None) == 409, e
    assert "already scanning" in str(getattr(e, "detail", "")), e
else:
    raise AssertionError("the rescan route swallowed slskd's refusal")

# the audit republishes slskd's words instead of an empty verdict
ready()
fake.statuses["GET /shares/contents"] = 503
audit = soulseek.share_audit(CFG, probe=True)
assert "slskd is unhappy" in audit["problems"][0]["message"], audit["problems"][0]
assert audit["ok"] is False

# ...and a live share state that cannot be read is not "fine"
ready()
fake.statuses["GET /shares"] = 500
audit = soulseek.share_audit(CFG)
assert audit["status"] == "not_running", audit["status"]
assert audit["problems"][0]["code"] == "share_state_unreadable"

print("ok  slskd's own errors are surfaced by the client, the route and the audit")

shutil.rmtree(_TMP, ignore_errors=True)
print("ok")
