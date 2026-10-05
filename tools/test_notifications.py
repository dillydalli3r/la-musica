#!/usr/bin/env python3
"""The emit sites behind the notification tray — one notification per outcome.

Every outcome the user asked to hear about is announced on the ONE bus
(`server/events.py`) and the client's tray turns each frame into exactly one
entry (tools/test_notifications.cjs). This suite covers the server half for the
sites that belong to this task:

* a finished script run (`server/main.py` `/api/run`) — the grader's own kind
  for a grade, `script_failed` when a script errored, `script_done` otherwise,
  and NOTHING for a run where every script was skipped (nothing happened);
* a newer release (`server/version.py`) — announced once per version, not once
  per check.

It also covers the SECOND transport the same frames ride (#48): Web Push, so a
device that is CLOSED still hears that an import finished. That half is pinned
against the specification rather than against itself — RFC 8291's own Appendix A
vector, replayed through the encryption this server sends — plus the fan-out,
the per-device kind filter, the 404/410 pruning RFC 8030 requires, and the one
rule that matters most: nothing in it may ever fail an event.

The client's own contract (payload shape, click targets, the badge, clear all)
lives in tools/test_notifications.cjs; this file is only about what the server
publishes.
"""
import base64
import json
import os
import struct
import sys
import tempfile
import threading
import time
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{('  — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILED.append(name)


from server import events as events_mod  # noqa: E402
from server import version as version_mod  # noqa: E402
from server import main as main_mod  # noqa: E402


def drain():
    """Forget every frame so far, so a count is about THIS outcome.

    BOTH stores go: `recent()` answers from the memory ring AND the durable log
    (see server/events.py — that is what a client closed overnight catches up
    from), so clearing one would leave the other's frames in the next count."""
    with events_mod._lock:
        events_mod._events.clear()
    try:
        os.remove(events_mod._event_log_path())
    except OSError:
        pass


def frames(kind=None):
    kept = events_mod.recent(0, limit=10 ** 6)
    return [e for e in kept if kind is None or e.get("event") == kind]


def shape_ok(frame, kind):
    """The payload the tray needs: kind, wording, a timestamp, the server's own
    event number, and a link the client can navigate to."""
    link = (frame.get("data") or {}).get("link")
    return (
        frame.get("event") == kind
        and bool(frame.get("title"))
        and isinstance(frame.get("body"), str)
        and float(frame.get("at") or 0) > 0
        and int(frame.get("seq") or 0) > 0
        and isinstance(link, str)
        and link.startswith("/")
    )


print("== an import that needs a human decision ==")
from server import import_autonomy  # noqa: E402
from mlo import paths as mlo_paths  # noqa: E402

data_dir = tempfile.mkdtemp(prefix="mlo-autonomy-")
mlo_paths.app_data_dir = lambda *a, **k: data_dir
album_dir = os.path.join(tempfile.mkdtemp(prefix="mlo-album-"), "An Album")
os.makedirs(album_dir, exist_ok=True)
missing = {"cover": {"id": "cover", "label": "Covers", "state": "source",
                     "fields": ["cover"], "note": ""}}
drain()
entry = import_autonomy.raise_prompt(album_dir, {}, missing, mode="automatic", reason="missing")
needs = frames("import_needs_data")
check("an album parked on a decision -> exactly one frame", len(needs) == 1)
check("...with the payload shape", shape_ok(needs[0], "import_needs_data") if needs else False)
check("...carrying the wizard link and the reason",
      bool(needs) and needs[0]["data"]["link"].startswith("/import?album=")
      and needs[0]["data"]["reason"] == "missing"
      and needs[0]["data"]["families"] == ["cover"])
check("...and the wizard can list the prompt",
      [e.get("album") for e in import_autonomy.prompts()] == [entry["album"].replace("\\", "/")])
check("...the tray's click target is the route the app mounts",
      bool(needs) and needs[0]["data"]["link"].split("?")[0] == "/import")
import_autonomy.raise_prompt(album_dir, {}, {}, mode="automatic", reason="missing")
check("an import that resolved the gap withdraws the prompt silently", len(frames("import_needs_data")) == 1)
check("...and the prompt is gone", import_autonomy.for_album(album_dir) == {})

print("== a finished script run (/api/run) ==")
import server.script_runners as script_runners  # noqa: E402

_real_chain = script_runners.run_chain
try:
    def stub(ids_results):
        def fake(cfg, ids, targets=None, force=None, progress=None, wait=False, timeout=None):
            script_runners._last_ids = list(ids)
            return [dict(r) for r in ids_results]
        script_runners.run_chain = fake

    stub([{"id": 4, "label": "Grade", "stats": {"grade_dist": {"PASS": 12, "FAIL": 1}}}])
    drain()
    main_mod._run_scripts(main_mod.RunRequest(ids=[4]))
    graded = frames("grade_done")
    check("a grade run -> exactly one grade_done", len(graded) == 1)
    check("...with the payload shape", shape_ok(graded[0], "grade_done") if graded else False)
    check("...reporting the pass/fail split and pointing at the library",
          bool(graded) and graded[0]["data"]["grade_dist"] == {"PASS": 12, "FAIL": 1}
          and graded[0]["data"]["link"] == "/library")
    check("...and no other kind", len(frames()) == 1)

    # A targeted grade is about ONE album: the notification opens that album.
    folder = str(main_mod.load_config().get("music_folder") or "")
    target = os.path.join(folder, "Some Album") if folder and os.path.isdir(folder) else r"F:\Music\A\One"
    stub([{"id": 4, "label": "Grade", "stats": {"grade_dist": {"PASS": 3, "FAIL": 2}}}])
    drain()
    main_mod._run_scripts(main_mod.RunRequest(ids=[4], targets=[target]))
    targeted = frames("grade_done")
    want = "/album/" + quote(os.path.normpath(target), safe="")
    check("a targeted grade links to the album it graded",
          len(targeted) == 1 and targeted[0]["data"]["link"] == want,
          (targeted[0]["data"] if targeted else {}).__str__())

    stub([{"id": 1, "label": "Format lyrics", "stats": {}},
          {"id": 3, "label": "Optimize FLACs", "stats": {}}])
    drain()
    main_mod._run_scripts(main_mod.RunRequest(ids=[1, 3]))
    done = frames("script_done")
    check("a successful chain -> exactly one script_done", len(done) == 1)
    check("...listing what ran", bool(done) and done[0]["data"]["ran"] == [1, 3])
    check("...linking to the in-progress view", bool(done) and done[0]["data"]["link"] == "/in-progress")

    stub([{"id": 8, "label": "Auto tagging", "error": "beets is not installed"}])
    drain()
    main_mod._run_scripts(main_mod.RunRequest(ids=[8]))
    failed = frames("script_failed")
    check("an errored run -> exactly one script_failed", len(failed) == 1)
    check("...naming the failure", bool(failed) and "beets" in failed[0]["body"]
          and failed[0]["data"]["failed"] == [8])
    check("...and never also a script_done", len(frames()) == 1)

    stub([{"id": 9, "label": "AccurateRip", "skipped": True, "reason": "grade_check_audit is off"},
          {"id": 16, "label": "Mood & Energy", "skipped": True, "reason": "off"}])
    drain()
    main_mod._run_scripts(main_mod.RunRequest(ids=[9, 16]))
    check("a run where nothing ran says nothing", frames() == [])
finally:
    script_runners.run_chain = _real_chain

print("== a run invalidates what it touched, not the whole library ==")

# Every run used to end with `tagcache.invalidate_all()`: the library page
# right after a ONE-ALBUM run re-parsed every track in the library with
# mutagen. A run that named its targets drops those folders' entries (plus the
# folders a script moved an album into), and only a run with no targets at all
# keeps the library-wide drop.
import server.mbresolve as mbresolve                 # noqa: E402
import server.tagcache as tagcache                   # noqa: E402

_folder = str(main_mod.load_config().get("music_folder") or "")
_target = os.path.join(_folder, "Some Album") if _folder else r"F:\Music\A\One"
_moved = os.path.join(_folder, "Some Album (2024)") if _folder else r"F:\Music\A\One (2024)"
calls = {"album": [], "all": 0, "mb": 0}
_real_album_inv = tagcache.invalidate_album
_real_all_inv = tagcache.invalidate_all
_real_mb_inv = mbresolve.invalidate


def _stub_chain(results):
    def fake(cfg, ids, targets=None, force=None, progress=None, wait=False,
             timeout=None):
        return [dict(r) for r in results]
    return fake


def _count_all():
    calls["all"] += 1


def _count_mb():
    calls["mb"] += 1


try:
    tagcache.invalidate_album = lambda *folders: calls["album"].append(list(folders))
    tagcache.invalidate_all = _count_all
    mbresolve.invalidate = _count_mb

    script_runners.run_chain = _stub_chain(
        [{"id": 3, "label": "Optimize FLACs", "stats": {"modified_count": 1}}])
    main_mod._run_scripts(main_mod.RunRequest(ids=[3], targets=[_target]))
    scoped = (list(calls["album"][-1]) if calls["album"] else None,
              calls["all"], calls["mb"])
    check("a one-album run drops THAT album's cached tags, not the whole library",
          scoped == ([os.path.normpath(_target)], 0, 1), str(scoped))

    # A script that MOVED the album (beets does, on every import) reports where
    # it put it: that folder's cache is stale too, and it is not one the caller
    # named.
    script_runners.run_chain = _stub_chain(
        [{"id": 14, "label": "Beets tagging",
          "stats": {"modified_count": 1, "moved_targets": [_moved]}}])
    main_mod._run_scripts(main_mod.RunRequest(ids=[14], targets=[_target]))
    check("...and the folders a script moved an album INTO are dropped with them",
          calls["album"][-1] == [os.path.normpath(_target), _moved],
          str(calls["album"][-1]))

    # A library-wide run really did touch everything, so it keeps the drop —
    # even when a script reported a folder it moved (the sweep's own writes are
    # not limited to that folder).
    script_runners.run_chain = _stub_chain(
        [{"id": 14, "label": "Beets tagging",
          "stats": {"modified_count": 9, "moved_targets": [_moved]}}])
    before = len(calls["album"])
    main_mod._run_scripts(main_mod.RunRequest(ids=[14]))
    check("a run with no targets at all still drops the whole library's cache",
          calls["all"] == 1 and len(calls["album"]) == before
          and calls["mb"] == 3,
          f"{calls}")
finally:
    tagcache.invalidate_album = _real_album_inv
    tagcache.invalidate_all = _real_all_inv
    mbresolve.invalidate = _real_mb_inv
    script_runners.run_chain = _real_chain

print("== a newer release ==")
tmp = os.path.join(tempfile.mkdtemp(prefix="mlo-notify-"), "update_check.json")
version_mod.cache_path = lambda: tmp
version_mod._fetch_latest = lambda: {"latest": "99.0.0", "release_url": "https://example.invalid/notes"}
drain()
first = version_mod.check(force=True)
upd = frames("update_available")
check("a fresh check that finds a release announces it once", len(upd) == 1)
check("...with the payload shape", shape_ok(upd[0], "update_available") if upd else False)
check("...naming both versions and the release page",
      bool(upd) and upd[0]["data"]["latest"] == "99.0.0" and upd[0]["data"]["url"]
      and upd[0]["data"]["link"] == "/settings")
check("...and the answer still reports the update", first.get("update_available") is True)
version_mod.check(force=True)
check("a second check does not repeat the news", len(frames("update_available")) == 1)
version_mod._fetch_latest = lambda: {"latest": "100.0.0", "release_url": ""}
version_mod.check(force=True)
check("a NEWER release is announced again", len(frames("update_available")) == 2)

# ── Web Push (issue #48) ────────────────────────────────────────────────────
#
# The transport that wakes a device whose app is closed. Everything here runs
# offline: the ONE network call the server makes (events._push_post) is
# replaced, and the frame it would have carried is opened exactly the way a
# browser's PushManager opens it (RFC 8291 §3.4) — which is the only way to
# prove the record is real without a push service to talk to.
print("== web push: waking a device that is closed ==")
try:
    from cryptography.hazmat.primitives import hashes as _hashes
    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    from cryptography.hazmat.primitives.asymmetric import utils as _asym
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey as _PubKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF as _HKDF
    crypto = True
except Exception as exc:  # pragma: no cover — a missing extra is a SKIP
    print(f"  SKIP  cryptography is not installed: {exc}")
    crypto = False

if crypto:
    from mlo import paths as mlo_paths  # noqa: E402
    from server import auth as auth_mod  # noqa: E402

    # Everything this section writes goes to its own directory: the key file and
    # the subscription rows must never land in the library's own .mlo (the same
    # reason the rest of this suite patches app_data_dir).
    push_dir = tempfile.mkdtemp(prefix="mlo-push-")
    mlo_paths.app_data_dir = lambda *a, **k: push_dir
    auth_mod.db_path = lambda: os.path.join(push_dir, "auth.db")
    auth_mod._initialized = False
    # Both caches are per process, and app_data_dir has already been moved by
    # an earlier section of this file — reset them so the pair is made HERE.
    events_mod._keys = None
    events_mod._signing_key_cache = None

    def b64u(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def unb64u(text):
        return base64.urlsafe_b64decode(str(text) + "=" * (-len(str(text)) % 4))

    class Device:
        """One browser: its own P-256 key pair and 16-byte authentication
        secret, subscribed through the real store, and the decryption a real
        PushManager performs on what arrives."""

        def __init__(self, kinds=None, endpoint="https://push.example.invalid/send/one",
                     username=""):
            self.key = _ec.generate_private_key(_ec.SECP256R1())
            self.public = self.key.public_key().public_bytes(
                _ser.Encoding.X962, _ser.PublicFormat.UncompressedPoint)
            self.secret = os.urandom(16)
            self.endpoint = endpoint
            auth_mod.push_subscribe(username, endpoint, b64u(self.public),
                                    b64u(self.secret), kinds or [])

        def open(self, body):
            """The record as a browser sees it: ECDH against the sender's
            public key, the same HKDF chain in reverse, AES-GCM."""
            salt, idlen = body[:16], body[20]
            as_public, ciphertext = body[21:21 + idlen], body[21 + idlen:]
            shared = self.key.exchange(
                _ec.ECDH(), _PubKey.from_encoded_point(_ec.SECP256R1(), as_public))
            ikm = _HKDF(_hashes.SHA256(), 32, salt=self.secret,
                        info=b"WebPush: info\x00" + self.public + as_public).derive(shared)
            cek = _HKDF(_hashes.SHA256(), 16, salt=salt,
                        info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
            nonce = _HKDF(_hashes.SHA256(), 12, salt=salt,
                          info=b"Content-Encoding: nonce\x00").derive(ikm)
            plain = _AESGCM(cek).decrypt(nonce, ciphertext, None)
            assert plain[-1] == 2, "the padding delimiter (RFC 8291 §4)"
            return json.loads(plain[:-1])

    posts = []

    def stub_push_service(status=201):
        """Stand in for the push service. The only network call in this half of
        the feature, replaced — never a real service."""
        def post(url, body, headers, timeout=10):
            posts.append({"url": url, "body": body, "headers": headers})
            return status, b""
        events_mod._push_post = post

    def settle():
        """Wait for the sender thread to finish what emit queued."""
        events_mod._push_queue.join()

    stub_push_service()
    # Every script-run section above published its own frame, and Web Push
    # delivery is asynchronous: a frame queued there is fanned out to whatever
    # device is subscribed WHEN THE WORKER RUNS, not when it was published.
    # Let that backlog empty BEFORE the first device subscribes — otherwise an
    # older frame lands in this section's `posts` and every count here is one
    # too high (the CI failure this prevents: posts[0] was the `grade_done`
    # "Grade finished" the /api/run section above published).
    settle()
    posts.clear()
    mine = Device(kinds=["import_done", "grade_done"])
    # An import whose summary grew an error list is the realistic way a frame
    # gets big: the record declares `rs` 4096, and a payload past it is a frame
    # a push service may refuse outright (RFC 8030 §7.2).
    events_mod.emit("import_done", "Imported An Album", "x" * 20000,
                    {"link": "/album/An%20Album"})
    settle()
    huge = mine.open(posts[0]["body"]) if posts else {}
    check("an oversized frame is trimmed under the record's own size",
          len(posts) == 1 and len(posts[0]["body"]) < events_mod._PUSH_RECORD_SIZE
          and len(huge.get("body", "")) > 0 and huge["body"].endswith("…"),
          str(len(posts[0]["body"]) if posts else 0))
    check("...keeping the title and the click target the user acts on",
          huge.get("title") == "Imported An Album"
          and (huge.get("data") or {}).get("link") == "/album/An%20Album", str(huge.get("title")))
    pub = events_mod.push_public_key()
    check("a VAPID key pair is generated on first use",
          len(unb64u(pub)) == 65 and unb64u(pub)[0] == 4, pub[:12])
    check("...once, not per send", events_mod.push_public_key() == pub)
    check("...kept beside the app's own state, not in the config",
          os.path.isfile(os.path.join(push_dir, "webpush.json")))

    # The encryption is checked against the SPEC, not against itself: if it ever
    # drifts, a push service refuses every record and the feature dies silently.
    kat_sender = _ec.derive_private_key(
        int.from_bytes(unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
        _ec.SECP256R1())
    kat = b64u(events_mod._encrypt(
        b"When I grow up, I want to be a watermelon",
        unb64u("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
        unb64u("BTBZMqHH6r4Tts7J_aSIgg"), kat_sender, unb64u("DGv6ra1nlYgDCS1FRnbzlw")))
    check("RFC 8291 Appendix A: the record is byte for byte the spec's",
          kat == ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIg"
                  "Dll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt"
                  "2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"))

    drain()
    posts.clear()
    events_mod.emit("import_done", "Imported Kind of Blue", "21 scripts ran",
                    {"album_path": "F:\\Music\\Miles - Kind of Blue"})
    settle()
    check("an import finishing -> one POST, to the subscribed device", len(posts) == 1)
    sent = posts[0] if posts else {"url": "", "body": b"", "headers": {}}
    check("...at the device's own endpoint", sent["url"] == mine.endpoint, sent["url"])
    check("...as an aes128gcm record with a TTL",
          sent["headers"].get("Content-Encoding") == "aes128gcm"
          and bool(sent["headers"].get("TTL")), str(sent["headers"]))
    frame = mine.open(sent["body"]) if sent["body"] else {}
    check("...carrying the frame the socket carries",
          frame.get("event") == "import_done" and frame.get("title") == "Imported Kind of Blue"
          and frame.get("body") == "21 scripts ran", str(frame))
    check("...with the album as the click target",
          (frame.get("data") or {}).get("album_path") == "F:\\Music\\Miles - Kind of Blue")

    auth_header = str(sent["headers"].get("Authorization") or "")
    jwt = auth_header.split("t=")[1].split(",")[0] if auth_header.startswith("vapid t=") else ""
    parts = jwt.split(".")
    claims, signed = {}, False
    if len(parts) == 3:
        claims = json.loads(unb64u(parts[1]))
        sig = unb64u(parts[2])
        try:
            _PubKey.from_encoded_point(_ec.SECP256R1(), unb64u(pub)).verify(
                _asym.encode_dss_signature(int.from_bytes(sig[:32], "big"),
                                           int.from_bytes(sig[32:], "big")),
                f"{parts[0]}.{parts[1]}".encode("ascii"), _ec.ECDSA(_hashes.SHA256()))
            signed = True
        except Exception:
            signed = False
    check("the POST is signed with the install's own VAPID key",
          auth_header.startswith("vapid t=") and auth_header.endswith("k=" + pub))
    check("...ES256, and it verifies against the published half", signed)
    check("...naming the push service it is for as its audience",
          claims.get("aud") == "https://push.example.invalid", str(claims))

    posts.clear()
    events_mod.emit("script_done", "Scripts finished", "3 ran")
    settle()
    check("a kind this device did not ask for does not wake it", posts == [], str(posts))
    events_mod.emit("grade_done", "Grade finished", "", {})
    settle()
    check("...one it did ask for does", len(posts) == 1)
    everything = Device(endpoint="https://push.example.invalid/send/two")
    posts.clear()
    events_mod.emit("script_done", "Scripts finished", "3 ran")
    settle()
    check("a device that asked for everything gets everything",
          [p["url"] for p in posts] == [everything.endpoint], str(posts))
    posts.clear()
    events_mod.emit("download_done", "Downloaded", "", {},
                    config={"notify_download_done": False})
    settle()
    check("a kind switched off in the config is published to nobody", posts == [], str(posts))

    posts.clear()
    test_result = events_mod.send_test("")
    check("the test button reaches this user's own devices",
          test_result["sent"] == 2 and test_result["subscriptions"] == 2
          and test_result["available"] is True, str(test_result))
    check("...including one that asked for other kinds only (a test must be "
          "deliverable where it was pressed)",
          {p["url"] for p in posts} == {mine.endpoint, everything.endpoint})
    opened = mine.open(posts[0]["body"]) if posts else {}
    check("...with its own wording and a click target",
          opened.get("event") == "test" and (opened.get("data") or {}).get("link") == "/settings")
    theirs = Device(kinds=["import_done"], endpoint="https://push.example.invalid/send/three",
                    username="ann")
    posts.clear()
    events_mod.send_test("ann")
    check("...and only to the caller's, never another user's",
          [p["url"] for p in posts] == [theirs.endpoint], str(posts))
    posts.clear()
    events_mod.emit("import_done", "Imported One", "", {})
    settle()
    check("an outcome still reaches every user's subscribed devices", len(posts) == 3)

    stub_push_service(410)
    events_mod.emit("import_done", "Imported Two", "", {})
    settle()
    check("a 410 drops the device (RFC 8030: it is gone)",
          auth_mod.push_subscription_count() == 0, str(auth_mod.push_subscription_count()))
    stub_push_service(404)
    Device(kinds=["import_done"], endpoint="https://push.example.invalid/send/four")
    events_mod.emit("import_done", "Imported Three", "", {})
    settle()
    check("...and so does a 404", auth_mod.push_subscription_count() == 0)
    stub_push_service(500)
    Device(kinds=["import_done"], endpoint="https://push.example.invalid/send/five")
    events_mod.emit("import_done", "Imported Four", "", {})
    settle()
    check("a push service having a bad day keeps the device",
          auth_mod.push_subscription_count() == 1)
    events_mod._push_post = lambda *a, **k: (0, b"")
    events_mod.emit("import_done", "Imported Five", "", {})
    settle()
    check("an unreachable push service keeps it too", auth_mod.push_subscription_count() == 1)

    def exploding_send(row, body):
        raise RuntimeError("push exploded")

    def exploding_store(*a, **k):
        raise RuntimeError("the store is gone")

    drain()
    real_send = events_mod._send_one
    events_mod._send_one = exploding_send
    published = events_mod.emit("import_done", "Imported Six", "summary", {"album_path": "X"})
    settle()
    check("a send that explodes still leaves the event published",
          published.get("event") == "import_done" and len(frames("import_done")) == 1)
    events_mod._send_one = real_send
    real_subscriptions = auth_mod.push_subscriptions
    auth_mod.push_subscriptions = exploding_store
    check("a subscription store that cannot be read is not an error either",
          events_mod.fanout({"event": "import_done"})
          == {"sent": 0, "gone": 0, "failed": 0, "subscriptions": 0})
    auth_mod.push_subscriptions = real_subscriptions

    kept = Device(kinds=["import_done"], endpoint="https://push.example.invalid/send/six")
    Device(kinds=["import_done"], endpoint="https://push.example.invalid/send/six",
           username="ann")
    check("re-subscribing one endpoint updates its row instead of adding one",
          auth_mod.push_subscription_count() == 2
          and [r["username"] for r in auth_mod.push_subscriptions()
               if r["endpoint"] == kept.endpoint] == ["ann"])
    check("a client may only drop its own device",
          auth_mod.push_unsubscribe(kept.endpoint, "bob") == 0
          and auth_mod.push_subscription_count() == 2)
    check("...its own goes through", auth_mod.push_unsubscribe(kept.endpoint, "ann") == 1)
    check("the server's own pruning is not scoped to a user",
          auth_mod.push_unsubscribe("https://push.example.invalid/send/five") == 1)
    Device(kinds=["import_done"], endpoint="https://push.example.invalid/send/seven",
           username="ann")
    auth_mod.revoke_all()
    check("signing out everywhere takes the devices with it (nothing is pushed to a "
          "signed-out phone)", auth_mod.push_subscription_count() == 0)

    auth_mod.push_subscribe("", "https://push.example.invalid/send/broken",
                            "not-a-key", "not-a-secret", [])
    posts.clear()
    stub_push_service()
    events_mod.emit("import_done", "Imported Seven", "", {})
    settle()
    check("a row whose keys cannot be used is pruned, not retried forever",
          posts == [] and auth_mod.push_subscription_count() == 0)

    print("== web push over HTTP: the routes the client calls ==")
    try:
        from fastapi.testclient import TestClient
    except Exception as exc:  # pragma: no cover
        print(f"  SKIP  TestClient unavailable: {exc}")
    else:
        # Gate OFF: this is about the push routes, not about who may call them
        # (tools/test_auth.py owns the gate). Both entry points are patched —
        # the middleware consults the cache first.
        gate_off = {"required": False, "mode": "auto", "has_password": False, "username": "",
                    "host": "127.0.0.1", "public_url": "", "session_days": 30}
        auth_mod.cached_state = lambda: dict(gate_off)
        auth_mod.current_state = lambda refresh=False: dict(gate_off)
        client = TestClient(main_mod.app)  # no `with`: no lifespan, no workers

        via_http = Device(kinds=["import_done"], endpoint="https://push.example.invalid/send/late")
        status_body = client.get("/api/push/status").json()
        check("the status route offers the key to subscribe with",
              status_body.get("available") is True and status_body.get("public_key") == pub
              and status_body.get("subscriptions") == 1, str(status_body))
        body = {"endpoint": "https://push.example.invalid/send/http",
                "keys": {"p256dh": b64u(via_http.public), "auth": b64u(via_http.secret)},
                "kinds": ["import_done"]}
        res = client.post("/api/push/subscribe", json=body)
        check("a subscription is accepted through the route",
              res.status_code == 200 and res.json().get("subscriptions") == 2, res.text)
        check("...with the kinds that device asked for",
              [r["kinds"] for r in auth_mod.push_subscriptions()
               if r["endpoint"] == body["endpoint"]] == ["import_done"])
        check("an endpoint that is not https is refused",
              client.post("/api/push/subscribe",
                          json=dict(body, endpoint="http://push.example.invalid/send/x")
                          ).status_code == 400)
        check("keys that are not a browser key pair are refused",
              client.post("/api/push/subscribe",
                          json=dict(body, keys={"p256dh": "AAAA", "auth": "AAAA"})
                          ).status_code == 400)
        posts.clear()
        res = client.post("/api/push/test", json={})
        check("the test route reports what it really did",
              res.status_code == 200 and res.json().get("sent") == 2, res.text)
        res = client.post("/api/push/unsubscribe", json={"endpoint": body["endpoint"]})
        check("the unsubscribe route forgets the device",
              res.status_code == 200 and res.json().get("removed") == 1
              and auth_mod.push_subscription_count() == 1, res.text)

        # The state every install is in for one release: requirements.txt names
        # `cryptography`, but a machine that has not re-run pip does not have it.
        # Push must be reported as unavailable rather than failing the API, the
        # event channel or the boot.
        live_keys = events_mod._keys
        events_mod._keys = {}
        try:
            check("without the crypto library the status route says so",
                  client.get("/api/push/status").json().get("available") is False)
            check("...a subscription is refused with 503, not a broken row",
                  client.post("/api/push/subscribe", json=body).status_code == 503)
            check("...and the test button says the same",
                  client.post("/api/push/test", json={}).status_code == 503)
            posts.clear()
            published = events_mod.emit("import_done", "Imported Anyway", "", {})
            settle()
            check("...while events keep being published, with no push attempted",
                  posts == [] and published.get("event") == "import_done")
        finally:
            events_mod._keys = live_keys

# ── The durable replay log ──────────────────────────────────────────────────
#
# The ring above is MEMORY. A client that was closed — a desktop or mobile shell
# no push service can reach — catches up with `?since=`, and that answer has to
# survive the process: the frames are on disk beside the push keys, and
# `recent()` reads both. The log is a catch-up window, not a ledger, so it keeps
# a bounded tail — the NEWEST frames, because a reconnect wants the afternoon,
# not the morning.
print("== the durable log: a frame outlives the memory ring ==")
log_dir = tempfile.mkdtemp(prefix="mlo-eventslog-")
mlo_paths.app_data_dir = lambda *a, **k: log_dir
_log_notify_devices = events_mod.notify_devices
events_mod.notify_devices = lambda payload: None
try:
    drain()
    frame = events_mod.emit("download_done", "Durable", "It survives the restart",
                            {"link": "/library"})
    with events_mod._lock:
        events_mod._events.clear()          # a restart: the ring is empty again
    replayed = frames("download_done")
    check("a frame survives the memory ring dying",
          len(replayed) == 1 and replayed[0]["seq"] == frame["seq"],
          json.dumps(replayed))
    check("...and it is the frame itself, payload included",
          shape_ok(replayed[0], "download_done") if replayed else False)
    check("...while a client that already saw it does not get it again",
          events_mod.recent(frame["at"], limit=10 ** 6) == [],
          json.dumps(events_mod.recent(frame["at"], limit=10 ** 6)))

    # The compaction keeps the newest `_LOG_KEEP` lines: the oldest frames are
    # what a bounded catch-up window drops.
    drain()
    seqs = [events_mod.emit("download_done", f"Durable {i}", "", {"link": "/library"})["seq"]
            for i in range(events_mod._LOG_KEEP + 5)]
    events_mod._log_compact(events_mod._event_log_path())
    with open(events_mod._event_log_path(), encoding="utf-8") as fh:
        kept = [json.loads(line) for line in fh if line.strip()]
    check("_log_compact keeps the newest _LOG_KEEP lines",
          [k["seq"] for k in kept] == seqs[-events_mod._LOG_KEEP:],
          json.dumps({"kept": len(kept), "keep": events_mod._LOG_KEEP}))
    check("...so the oldest frames are the ones that go",
          seqs[0] not in [k["seq"] for k in kept]
          and seqs[-1] in [k["seq"] for k in kept],
          json.dumps({"first": seqs[0], "last": seqs[-1], "kept": len(kept)}))
finally:
    events_mod.notify_devices = _log_notify_devices

# ── The library-changed signal ──────────────────────────────────────────────
#
# Every mutating request that can touch the library publishes ONE coalesced
# `library_changed` frame, so an open page drops its library-derived queries —
# including a page that did not make the write, and a second device. Paths that
# cannot change the library (credentials, config, the per-user stores) publish
# nothing, and a GET never does. The client treats the kind as silent (no tray
# row, no OS popup) and refreshes on it — tools/test_notifications.cjs owns that
# half.
print("== the library-changed signal ==")
check("a tag write is a library write", events_mod.affects_library("/api/mb/assign"))
check("a cover write is a library write", events_mod.affects_library("/api/cover/fromurl"))
check("a trash move is a library write", events_mod.affects_library("/api/trash/delete"))
check("a script run is a library write", events_mod.affects_library("/api/run"))
check("a config write is NOT", not events_mod.affects_library("/api/config"))
check("auth is NOT", not events_mod.affects_library("/api/auth/login"))
check("a rating is NOT", not events_mod.affects_library("/api/ratings"))
check("the wizard's bookmark is NOT", not events_mod.affects_library("/api/import/sessions"))

drain()
events_mod.note_library_write("/api/mb/assign")
events_mod.note_library_write("/api/cover/fromurl")   # the same burst
time.sleep(events_mod._LIB_CHANGED_DELAY + 0.4)
burst = frames("library_changed")
check("a burst of writes is ONE frame (coalesced)", len(burst) == 1, json.dumps(burst))

try:
    from fastapi.testclient import TestClient
    from server import auth as _auth
except Exception as exc:  # pragma: no cover
    print(f"  SKIP  TestClient/auth unavailable: {exc}")
else:
    # Gate OFF (about the write signal, not who may call the route) and a
    # scratch music folder (the route guards its folder), both restored below.
    _gate_off = {"required": False, "mode": "auto", "has_password": False, "username": "",
                 "host": "127.0.0.1", "public_url": "", "session_days": 30}
    _cached, _current = _auth.cached_state, _auth.current_state
    _real_load_cfg = main_mod.load_config
    _lib_dir = tempfile.mkdtemp(prefix="mlo-libchg-")
    _auth.cached_state = lambda: dict(_gate_off)
    _auth.current_state = lambda refresh=False: dict(_gate_off)
    main_mod.load_config = lambda: {"music_folder": _lib_dir}
    try:
        client = TestClient(main_mod.app)  # no `with`: no lifespan, no workers
        drain()
        res = client.post("/api/mb/assign", json={"tracks": {}})
        time.sleep(events_mod._LIB_CHANGED_DELAY + 0.4)
        check("a successful mutating request publishes the frame through HTTP",
              res.status_code < 400 and len(frames("library_changed")) == 1,
              f"{res.status_code} {res.text[:200]} {json.dumps(frames('library_changed'))}")
        drain()
        client.get("/api/health")
        time.sleep(events_mod._LIB_CHANGED_DELAY + 0.4)
        check("a GET publishes nothing",
              frames("library_changed") == [], json.dumps(frames("library_changed")))
    finally:
        _auth.cached_state, _auth.current_state = _cached, _current
        main_mod.load_config = _real_load_cfg

# ── A rebuild must END on the tree that includes the last write (R348) ───────
#
# `invalidate_album`/`invalidate_path` serve the tree stale-while-revalidate, so
# a script writing for minutes can have a background rebuild FINISH mid-write.
# That rebuild must not store its pre-write rows and clear the dirty flag: the
# write that landed after it started would never be rebuilt, and the page would
# sit on the stale tree until the next visit — the "the library does not refresh
# after a script runs" report.
print("== a rebuild ends on the tree that includes the last write ==")
from server import tagcache as tc  # noqa: E402

_KEY = "test-library-key"


def _fresh_cache():
    with tc._lock:
        tc._lib_cache.clear()
        tc._lib_body.clear()
        tc._lib_dirty.clear()
        tc._lib_refreshing.clear()
        tc._lib_building.clear()
        tc._lib_write_gen = 0
        tc._lib_drop_gen = 0


def _await_refresh():
    for _ in range(400):
        with tc._lock:
            if _KEY not in tc._lib_refreshing:
                return
        time.sleep(0.02)


# Control: nothing changes while the rebuild runs -> stored AND dirty cleared.
_fresh_cache()
with tc._lock:
    tc._lib_cache[_KEY] = (time.time(), {"v": 0})
tc.invalidate_album("/music/Artist/Album")
tc._refresh_library(_KEY, lambda: {"v": 1}, notify=True)
_await_refresh()
with tc._lock:
    _stored = tc._lib_cache.get(_KEY, (0.0, None))[1]
    _dirty = _KEY in tc._lib_dirty
check("a rebuild with nothing in flight stores its tree and clears the dirty flag",
      _stored == {"v": 1} and not _dirty, json.dumps({"stored": _stored, "dirty": _dirty}))

# A WRITE lands while the rebuild runs: the flag must stay set so the next
# request rebuilds again (and the rebuild's own frame brings a client back).
_fresh_cache()
with tc._lock:
    tc._lib_cache[_KEY] = (time.time(), {"v": 0})
tc.invalidate_album("/music/Artist/Album")
_go, _release = threading.Event(), threading.Event()


def _slow_builder():
    _go.set()
    _release.wait(5)
    return {"v": 1}


tc._refresh_library(_KEY, _slow_builder, notify=True)
_go.wait(5)
tc.invalidate_album("/music/Artist/Album")   # the write that lands mid-build
_release.set()
_await_refresh()
with tc._lock:
    _dirty2 = _KEY in tc._lib_dirty
check("a write during the rebuild keeps the tree dirty (the next request rebuilds it)",
      _dirty2, json.dumps({"dirty": _dirty2}))

# A DROP lands while the rebuild runs (a settings change, the Refresh button):
# the payload came from a library the caller threw away, so it must NOT be
# stored — resurrecting it would serve a tree built with the OLD config.
_fresh_cache()
with tc._lock:
    tc._lib_cache[_KEY] = (time.time(), {"v": 0})
tc.invalidate_album("/music/Artist/Album")
_go2, _release2 = threading.Event(), threading.Event()


def _slow_builder2():
    _go2.set()
    _release2.wait(5)
    return {"v": 9}


tc._refresh_library(_KEY, _slow_builder2, notify=True)
_go2.wait(5)
tc.invalidate_all()          # the drop
_release2.set()
_await_refresh()
with tc._lock:
    _present = _KEY in tc._lib_cache
check("a drop during the rebuild discards the in-flight tree (it must not resurrect)",
      not _present, json.dumps({"present": _present}))

_fresh_cache()

print(f"\n{len(FAILED)} failure(s)")
sys.exit(1 if FAILED else 0)
