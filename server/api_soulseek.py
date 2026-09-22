"""GET /api/soulseek/port-check — the "Test port" probe for the listen port.

The one thing the Soulseek tab cannot work out for itself: whether the port peers
connect to can be reached, checked as far as it CAN be checked from this machine
(`server.soulseek_port`). The answer is a list of rows — the listener here, what
the router holds for the port, the addresses both depend on, a connection from
this machine to the public address, and slskd's own login state — and every row
carries what it proves and what it cannot, because a definite "open to the
internet" needs a probe from OUTSIDE this network, which this app does not ship.

Read-only and lock-free on purpose: no mapping is added or removed by asking, and
the probe can be asked while a download is running.

The router is registered by server/main.py (`include_router`); this module never
imports it.
"""
from fastapi import APIRouter

from server import soulseek_port

router = APIRouter()


@router.get("/api/soulseek/port-check")
def soulseek_port_check():
    """What this machine can prove about the Soulseek listen port, right now.

    ``verdict`` is the worst row's state, or ``ok`` only when the two links that
    really can be proven here are both green (something accepts on the port, and
    the router lists a mapping for it). ``note`` is the one thing no row can say:
    the outside half of the answer needs a probe from outside, which this app does
    not ship. Every network step is bounded, and nothing here waits on a lock, so
    the button works while transfers are running.
    """
    return soulseek_port.port_check()
