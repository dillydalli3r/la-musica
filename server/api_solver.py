"""The Cloudflare solver — GET /api/solver, POST /api/solver/{start,stop,config}.

FlareSolverr is installed from Dependencies (its own row, `flaresolverr`) and
RUN by this app (`server/solver.py`), so nothing in Settings has to know how a
process is spawned or where the URL comes from — this router is the thin HTTP
face of that module, plus the one config write the Solver card needs.

The Install button does NOT live here: it reuses
`POST /api/dependencies/install {"keys": ["flaresolverr"]}`, the endpoint that
already streams the download, reports the version and returns the refreshed
row. A second installer on this router would be a second place for those
sentences to drift, and this module adds no storage of its own either — the one
key it writes (`cf_solver_autostart`) goes through the app's own config writer,
so the Settings form, a hand-edited config.json and this route cannot disagree.

Mounted by server/main.py (``include_router``); this module never imports it.
"""
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from mlo.config import load_config, save_config
from server import solver

router = APIRouter(tags=["solver"])


class SolverConfigBody(BaseModel):
    """The two things the card can write: the autostart switch, and — only for a
    solver that runs SOMEWHERE ELSE — an explicit URL override.

    Both are optional so a write only touches what it names: the card's
    checkbox must not blank an override someone set, and clearing the override
    must not flip the switch. `cf_solver_url` is left empty by default and the
    app finds the solver itself (server/solver.py), so this field exists for the
    one case the automatic resolution cannot cover — a solver on another
    machine or another port — and is not part of the normal setup.
    """

    enabled: Optional[bool] = None
    url: Optional[str] = None


def _payload(message: str = "", ok: Optional[bool] = None):
    """The card's response: the fresh status, plus the action's own outcome.

    Every route answers with the status the card renders, so a Start that
    worked (or a Stop that was never ours) settles the card in place instead of
    the page waiting a round trip to learn what happened.
    """
    body = {"status": solver.status()}
    if ok is not None:
        body["ok"] = bool(ok)
        body["message"] = message
    return body


@router.get("/api/solver")
def solver_status():
    """Installed? running? which URL and where did it come from? — the card."""
    return _payload()


@router.post("/api/solver/start")
def solver_start():
    """Start the managed solver (or report that one already answers)."""
    ok, message = solver.start()
    return _payload(message, ok)


@router.post("/api/solver/stop")
def solver_stop():
    """Stop the solver THIS app started; a sidecar or a hand-run one stays."""
    ok, message = solver.stop()
    return _payload(message, ok)


@router.post("/api/solver/config")
def solver_config(req: SolverConfigBody):
    """Persist the autostart switch and/or the URL override.

    The install and start paths need no URL at all — that is the point of the
    card — so a write here is either that switch or the deliberate override for
    a solver this machine does not run.
    """
    cfg = load_config()
    if req.enabled is not None:
        cfg["cf_solver_autostart"] = bool(req.enabled)
    if req.url is not None:
        cfg["cf_solver_url"] = str(req.url or "").strip()
    if not save_config(cfg):
        reason = getattr(save_config, "last_error", "") or ""
        return _payload(f"could not save the setting ({reason})".strip(), False)
    return _payload("saved", True)