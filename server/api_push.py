"""Web Push — the transport behind ``server/events.py``.

The device half of the event channel: a browser subscribes here and is then
woken with the same frames ``/ws/events`` carries, with the app closed. The
routes are thin on purpose — the key material, the encryption and the fan-out
all live in ``server/events.py``, and the subscription rows in
``server/auth.py``'s database — so there is exactly one place that knows how a
push is sent.
"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from server import auth as auth_mod
from server import events as events_mod

router = APIRouter(tags=["push"])


# --------------------------------------------------------------------------- #
# Web Push (the transport behind server/events.py)
# --------------------------------------------------------------------------- #
# The device half of the event channel: a browser subscribes here and is then
# woken with the same frames /ws/events carries, with the app closed. The
# routes are thin on purpose — the key material, the encryption and the fan-out
# all live in server/events.py, and the subscription rows in server/auth.py's
# database — so there is exactly one place that knows how a push is sent.
class PushSubscription(BaseModel):
    endpoint: str
    keys: dict
    # What this device asked to be woken for; empty/absent means "everything
    # the server publishes".
    kinds: Optional[List[str]] = None


class PushEndpoint(BaseModel):
    endpoint: str


@router.get("/api/push/status")
def push_status(request: Request):
    """Whether push can be offered here, the key to subscribe with, and how
    many devices this user already has registered.

    A client asks BEFORE it shows the switch: an install whose `cryptography`
    is missing, or a deployment that has no key material, must not be offered a
    button that cannot work (see lib/notify.ts).
    """
    user = auth_mod.current_user(request)
    return {
        "available": bool(events_mod.push_public_key()),
        "public_key": events_mod.push_public_key(),
        "subscriptions": auth_mod.push_subscription_count(user),
    }


@router.post("/api/push/subscribe")
def push_subscribe(body: PushSubscription, request: Request):
    """Register (or refresh) this device.

    The endpoint and the two keys come from the browser's own PushManager; the
    row is scoped to the caller's user, so the news lands on the devices of the
    person who set them up. Re-posting the same endpoint updates it in place —
    that is what the client does on every load (a browser rotates its key
    material, and a stale row would encrypt to a key nobody holds any more).
    """
    endpoint = (body.endpoint or "").strip()
    keys = body.keys or {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth_key = str(keys.get("auth") or "").strip()
    # A push endpoint is always https (RFC 8030 §5: the push service is reached
    # over TLS), and the keys are a P-256 point and a 16-byte secret. Rejecting
    # the rest here keeps an unusable row out of the database — `_send_one`
    # would only discover it after the next import finished.
    if not endpoint.startswith("https://") or len(endpoint) > 2048:
        raise HTTPException(400, "not a push endpoint")
    if not events_mod.key_size_ok(p256dh, 65) or not events_mod.key_size_ok(auth_key, 16):
        raise HTTPException(400, "not a browser key pair")
    if not events_mod.push_public_key():
        raise HTTPException(503, "push is unavailable on this server")
    user = auth_mod.current_user(request)
    auth_mod.push_subscribe(user, endpoint, p256dh, auth_key, body.kinds)
    return {"ok": True, "subscriptions": auth_mod.push_subscription_count(user)}


@router.post("/api/push/unsubscribe")
def push_unsubscribe(body: PushEndpoint, request: Request):
    """Forget this device (the switch turned off, or a sign-out).

    Scoped to the caller: one user cannot unregister another's device.
    """
    removed = auth_mod.push_unsubscribe(
        (body.endpoint or "").strip(), auth_mod.current_user(request))
    return {"ok": True, "removed": removed}


@router.post("/api/push/test")
def push_test(request: Request):
    """Send one test frame to THIS user's devices.

    The button that lets somebody prove it on their own phone instead of
    believing a settings page. It answers what really happened — how many
    devices took it, how many were dead and how many failed — because "sent"
    with a silent zero is exactly the feedback this feature must not give.
    """
    result = events_mod.send_test(auth_mod.current_user(request))
    if not result.get("available"):
        raise HTTPException(503, "push is unavailable on this server")
    return result
