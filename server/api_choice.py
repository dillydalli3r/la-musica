"""GET /api/mb/release-choice — which edition of a release group the app takes.

The one answer the UI cannot work out for itself: the release-choice policy
(``mlo.release_choice``) ranks MusicBrainz's editions of a release group and
says WHY, so every place that offers "add this album" can show the pick, the
alternatives and the reasons — the same pick the wish queue, the auto-import
and the artist watch will make, because they all call that one policy.

One browse per release group (``integrations.release_group_browse``: cached and
throttled by the app's shared MusicBrainz access), never one request per
release. The config is loaded here because the policy itself is pure — payloads
plus a config dict, nothing else.

The router is registered by server/main.py (``include_router``); this module
never imports it.
"""
from fastapi import APIRouter, HTTPException, Query

from mlo import release_choice
from mlo.config import load_config

router = APIRouter()


@router.get("/api/mb/release-choice")
def mb_release_choice(release_group_mbid: str = Query(""),
                      prefer: str = Query(""),
                      primary_type: str = Query(""),
                      secondary_type: str = Query("")):
    """The ranked editions of a release group, best first, with the reasons.

    ``prefer`` names a release the caller wants instead of the policy's pick —
    the release page's own rows always know which edition the user clicked, and
    the answer says that is why it was chosen. ``primary_type`` /
    ``secondary_type`` are the release-group type the caller is after (the same
    names ``/api/mb/search`` takes, from ``mlo.naming``'s vocabulary): a page
    asking for albums is told which album edition to take, and a single is not
    offered in its place. Both empty = no type filter, and the group's own type
    is echoed in the response.
    """
    from server import integrations as intg

    rid = intg._mbid(release_group_mbid)
    if not rid:
        raise HTTPException(400, "a MusicBrainz release-group ID or URL is required")
    # "-" is the "no override" sentinel the query spells; an empty value means
    # the same thing.
    prefer = "" if str(prefer or "").strip() in ("", "-") else prefer
    try:
        group = intg.release_group_browse(rid, limit=100, offset=0)
    except Exception as e:
        # A MusicBrainz outage is not "no such release group".
        raise HTTPException(502, f"MusicBrainz release-group lookup failed: {e}")
    if not group.get("id"):
        raise HTTPException(404, "MusicBrainz knows no such release group")

    body = release_choice.choice_payload(
        group["id"], group, group.get("releases") or [], load_config(),
        prefer=prefer, primary_type=primary_type, secondary_type=secondary_type,
    )
    wanted = str(prefer or "").strip().lower()
    if wanted and body["chosen"] is None:
        # The id is not one of this group's editions: say so instead of
        # silently answering with a pick the caller did not ask for.
        raise HTTPException(404, "MusicBrainz does not list that release in "
                                 "this release group")
    return body
