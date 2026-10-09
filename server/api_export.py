"""Export to device: the DAP/USB export wizard's routes.

GET /api/export/defaults|configs|structures|files|drives|codecs|eq and the
POST/DELETE ones behind them: the option dialog's own vocabulary, the saved
export configs (``server.exportconfigs``), the structure preview, the EQ
profiles the player carries (``mlo.eq``), and the run itself — which is
``server.exporter`` copying/transcoding onto the target drive, claimed per
path through ``server.job_locks``.
"""
import os
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from mlo import eq as eq_mod
from mlo.config import DEFAULT_CONFIG
from server import exportconfigs, exporter, job_locks
from server.api_common import _in_music_folder, _music_folder, load_config

router = APIRouter(tags=["export"])


# --------------------------------------------------------------------------- #
# Export to device (MP3 player / DAP / USB drive)
# --------------------------------------------------------------------------- #
class ExportRequest(BaseModel):
    paths: List[str] = []              # absolute audio file paths to export
    dest: str = ""                     # destination drive root (e.g. "E:\\")
    subfolder: str = "Music"           # created under the drive root
    codec: str = "copy"                # any key of exporter.CODECS
    quality: str = ""                  # a preset key (V0/320/256/q8/…) or a number
    # Which tree an exported track lands in: a key of exporter.STRUCTURES
    # ("" = the shipped one), "custom" for the script below. The exporter is
    # the authority — an unknown key, a bad %field% or a script that names no
    # path comes back as a 400 sentence.
    structure: str = ""
    # The naming script a "custom" structure evaluates (the same %field% /
    # $if() grammar the library's own naming script uses, mlo.naming).
    structure_script: Optional[str] = None
    # Compatibility options. None = use the saved `export_*` config value, so a
    # client that omits a field keeps the user's defaults instead of forcing
    # the built-in one (server.exporter.EXPORT_DEFAULTS holds both).
    embed_covers: Optional[bool] = None
    embed_cover_jpeg_quality: Optional[int] = None
    embed_cover_resolution: Optional[int] = None
    id3v2: Optional[str] = None
    id3v1: Optional[bool] = None
    # "off" | "tags" (write the ReplayGain tags) | "apply" (rewrite the audio).
    replaygain_mode: Optional[str] = None
    eq_profile: Optional[str] = None   # a preset/profile id, "" = no EQ
    # "embedded" (the LYRICS tag) | "lrc" (a .lrc beside the file) | "both" |
    # None = the saved export_lyrics, else the library's own lyrics_format.
    lyrics: Optional[str] = None
    clean_tags: Optional[bool] = None
    playlists: Optional[bool] = None
    sidecars: Optional[bool] = None
    # WHICH files the run writes: the family keys of exporter.FILE_FAMILIES
    # ("" / absent = the saved export_copy_files, else the `sidecars` switch
    # above, else the tracks alone). The exporter is the authority — an unknown
    # family and an EMPTY selection ([] would write an empty folder) both come
    # back as a 400 sentence.
    copy_files: Optional[List[str]] = None
    manifest: Optional[bool] = None
    verify: Optional[bool] = None
    prune: Optional[bool] = None
    workers: Optional[int] = None
    # "server" writes into dest/subfolder (needs a destination the SERVER can
    # see), "zip" stages the same export under the app's data dir and hands
    # back one archive — the only destination a browser can offer its user.
    target: Optional[str] = None


class EqImportRequest(BaseModel):
    name: str = ""
    text: str = ""

class EqAutoEqRequest(BaseModel):
    # The results-relative directory the search returned
    # (`<source>/<rig>/<model>`), not a URL: the server builds the raw GitHub
    # URL itself and refuses anything that is not a plain relative path.
    id: str = ""
    # Which AutoEq form to fetch first — "parametric" (its own parametric
    # output), "graphic" (the band list it is derived from) or "fixed".
    form: str = "parametric"


class ExportConfigRequest(BaseModel):
    name: str = ""
    # The Export page's form, as the page holds it. A dict rather than one field
    # per option on purpose: the form IS the schema, and
    # server.exportconfigs validates it against the exporter's own tables
    # (EXPORT_DEFAULTS, CODECS, STRUCTURES) — a Pydantic copy here would be a
    # second schema to keep in step with the run.
    config: dict = {}


class StructurePreviewRequest(BaseModel):
    script: str = ""
    ext: str = ""      # the codec's produced extension, for the example path


@router.get("/api/export/defaults")
def export_defaults():
    """The saved export form values — what the Export page loads when it opens
    and what its "Save as default" writes back."""
    cfg = load_config()
    out = {}
    for name in ("dest", "subfolder", "codec", "quality", "structure",
                 "structure_script"):
        out[name] = cfg.get("export_" + name, DEFAULT_CONFIG.get("export_" + name, ""))
    for name, default in exporter.EXPORT_DEFAULTS.items():
        out[name] = cfg.get("export_" + name, default)
    # Lyrics are the one option whose shipped default is NOT a constant: with
    # nothing saved it is the library's own `lyrics_format`, so the form opens
    # showing what the app keeps in the library (see exporter.lyrics_mode) and
    # the select always has a value that matches one of its options.
    out["lyrics"] = exporter.lyrics_mode(cfg, {})
    # …and the file selection is resolved the same way (the run's own resolver,
    # not the raw key): a config that still holds only `export_sidecars` opens
    # the form showing the set that switch stands for, so the page always shows
    # the files the next export would actually write.
    out["copy_files"] = list(exporter.copy_files(cfg, {}))
    return out


@router.get("/api/export/configs")
def export_configs_list():
    """Every saved export configuration, newest first.

    Each row carries the equalizer profile it names and whether that profile is
    still there (`eq_missing` / `eq_problem`): a config outlives the profile it
    was saved with, so the list has to be able to mark one as broken before the
    user picks it."""
    return {"configs": exportconfigs.list_configs(_music_folder())}


@router.post("/api/export/configs")
def export_configs_save(req: ExportConfigRequest):
    """Save the Export page's form under a name (or replace that name's config).

    A name that could name a path outside the config folder, an unknown field,
    and a value a run would refuse (an unknown codec, folder structure, target
    or ReplayGain mode) are 400s: a saved config must not be a way to store
    something an export cannot do."""
    try:
        return exportconfigs.save(_music_folder(), req.name, req.config)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/export/configs/{config_id}")
def export_configs_load(config_id: str):
    """One saved config: the form values a run takes, plus the state of the
    equalizer profile it names.

    A config whose profile has been renamed or deleted comes back with
    `eq_missing` true and `eq_problem` filled — the UI says so and the run
    refuses that profile, rather than the export quietly using another curve. A
    config that is not there is a 404 (the UI may be showing a stale list); an
    id that could name another file is a 400."""
    try:
        row = exportconfigs.load(_music_folder(), config_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if row is None:
        raise HTTPException(404, f"no such saved config: {config_id}")
    return row


@router.delete("/api/export/configs/{config_id}")
def export_configs_delete(config_id: str):
    """Drop one saved configuration. Not there = 404; a path-shaped id = 400."""
    try:
        removed = exportconfigs.delete(_music_folder(), config_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not removed:
        raise HTTPException(404, f"no such saved config: {config_id}")
    return {"ok": True, "id": config_id}


@router.get("/api/export/structures")
def export_structures():
    """The folder-structure menu (keys and labels) and the %fields% /
    $functions a custom structure script may use — the Exporter's own tables,
    so the dropdown cannot offer a structure the run would refuse."""
    return exporter.structure_menu()


@router.get("/api/export/files")
def export_files():
    """The file families a run can be asked to copy (keys, labels, hints) — the
    Exporter's own table, so the checkboxes cannot offer a family the run would
    refuse, and the sentence a refused selection comes back with names the same
    vocabulary."""
    return exporter.file_families()


@router.post("/api/export/structure/preview")
def export_structure_preview(req: StructurePreviewRequest):
    """What a user-typed folder structure would write, for one sample track.

    The Export page calls this while the custom structure is being typed: the
    same grammar and the same validation the run itself applies, so a field the
    app does not know is refused there with the server's own sentence instead
    of being discovered at the end of a run."""
    return exporter.preview_structure(req.script, req.ext)


@router.get("/api/export/drives")
def export_drives():
    """Candidate destination drives with free space and bus type."""
    try:
        return {"drives": exporter.list_drives()}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/api/export/codecs")
def export_codecs():
    """Export codecs with the quality presets and custom-value ranges the
    Export page offers — the server's tables are the single source of truth,
    so adding a codec needs no UI change."""
    return {"codecs": exporter.codec_specs()}


@router.get("/api/export/eq")
def export_eq():
    """The equalizer profiles an export can carry: the built-in presets plus
    everything the user imported, with the filter chain each one renders to."""
    return eq_mod.catalog(_music_folder())


@router.post("/api/export/eq/import")
def export_eq_import(req: EqImportRequest):
    """Store one pasted Equalizer APO / Peace profile and return its row.

    A name that could name a path outside the profile folder, a name that is a
    built-in preset's, or a body past the size cap is a 400 — never a file
    written somewhere else."""
    try:
        return eq_mod.import_profile(_music_folder(), req.name, req.text)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/export/eq/{profile_id}")
def export_eq_delete(profile_id: str):
    """Drop one imported profile. A profile that is not there is a 404 (the UI
    may be showing a stale list); a name that could name another file is a 400."""
    try:
        removed = eq_mod.delete_profile(_music_folder(), profile_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not removed:
        raise HTTPException(404, f"no such profile: {profile_id}")
    return {"ok": True, "id": profile_id}


@router.get("/api/eq/autoeq/search")
def eq_autoeq_search(q: str = Query(""), limit: int = Query(40),
                     refresh: bool = Query(False)):
    """Search AutoEq's measured headphones by name.

    The catalogue itself is the project's INDEX.md, cached beside the profiles
    for a month (mlo.eq.autoeq_index): fetching a megabyte per keystroke is not
    a search box. `refresh=1` re-fetches it — the button the page offers when a
    headphone the user owns is missing — and a failed refresh still answers with
    the cached rows, with the reason in `error`."""
    index = eq_mod.autoeq_index(_music_folder(), refresh=bool(refresh))
    rows = eq_mod.autoeq_search(q, index["rows"], limit=max(1, min(200, int(limit or 40))))
    return {"rows": rows, "models": len(index["rows"]),
            "fetched_at": index["fetched_at"], "error": index["error"]}


@router.post("/api/eq/autoeq/import")
def eq_autoeq_import(req: EqAutoEqRequest):
    """Fetch one AutoEq correction and store it as a profile.

    The row that comes back is the stored profile — the same shape every other
    profile has, so the caller can select it immediately. An id that is not a
    results-relative directory, a fetch that fails, or a file this parser will
    not accept is a 400/502 with the reason; the profile is never stored half
    read."""
    try:
        return eq_mod.autoeq_import(_music_folder(), req.id, req.form)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"AutoEq fetch failed: {e}")


@router.get("/api/export/zip/{zip_id}")
def export_zip_download(zip_id: str):
    """Stream the archive a zip export built.

    An unknown id (a restart, a newer export that replaced it) is a 404 rather
    than an empty file: the client can say the export is gone instead of saving
    a zero-byte download. The response carries the archive's own human name as
    the downloaded file name."""
    path = exporter.zip_path(load_config(), zip_id)
    if not path:
        raise HTTPException(404, "no export archive with that id")
    return FileResponse(path, media_type="application/zip",
                        filename=os.path.basename(path))


@router.delete("/api/export/zip/{zip_id}")
def export_zip_delete(zip_id: str):
    """Drop the built archive early (it is replaced by the next zip export)."""
    if not exporter.drop_zip(load_config(), zip_id):
        raise HTTPException(404, "no export archive with that id")
    return {"ok": True, "id": zip_id}


def _export_claim_paths(req):
    """What one export HOLDS for the whole call: the source tracks, and the
    destination it writes into.

    The sources were always held (an export reads the library files it was
    pointed at, and a script run rewriting them mid-copy is the collision the
    lock exists to prevent). The DESTINATION was not, and that is a real race
    the owner asked about: two exports into one root run `_copy_once` over the
    same names at once, and the second run's `prune` deletes audio the first
    just wrote. A destination outside the library never collides with a script
    run, so holding it costs nothing elsewhere — and two exports to one drive
    now answer 409 instead of fighting."""
    paths = list(getattr(req, "paths", None) or [])
    target = str(getattr(req, "target", "") or "server").strip().lower()
    if target == "server" and str(getattr(req, "dest", "") or "").strip():
        sub = str(getattr(req, "subfolder", "") or "").replace("\\", "/").strip("/")
        paths.append(os.path.join(str(req.dest), *([sub] if sub else [])))
    return paths


@router.post("/api/export/cancel")
def export_cancel():
    """Ask the running export to stop (see exporter.request_cancel).

    Stops at the next file boundary, keeps everything already written, and
    reports `cancelled: true` in that run's result. `cancelled` here answers
    whether a run was in flight at all — a press when nothing is exporting says
    so instead of pretending."""
    stopped = exporter.request_cancel()
    return {"ok": True, "cancelled": stopped}


@router.post("/api/export")
@job_locks.holds(_export_claim_paths, kind="export", label="Export")
def export_run(req: ExportRequest):
    """Copy/transcode the selected tracks onto the target drive. Runs in the
    worker thread pool (sync def) and reports progress via the shared hook,
    so the header progress bar behaves exactly like a library script run.

    The SOURCE tracks and the DESTINATION are held (see _export_claim_paths)."""
    if not req.paths:
        raise HTTPException(400, "no tracks selected")
    target = (req.target or "server").strip().lower()
    if target not in exporter.TARGETS:
        raise HTTPException(400, f"unknown export target: {req.target}")
    dest_root = ""
    if target == "server":
        # A zip export ignores dest (it stages under the app's own data dir),
        # so only the drive target has a destination to check.
        dest_root = os.path.abspath(req.dest)
        if not os.path.isdir(dest_root):
            raise HTTPException(400, f"destination not found: {req.dest}")
    for p in req.paths:
        if not _in_music_folder(p, _music_folder()):
            raise HTTPException(400, f"file outside music folder: {p}")
    if req.codec not in exporter.CODECS:
        raise HTTPException(400, f"unknown codec: {req.codec}")
    cfg = load_config()
    opts = {k: v for k, v in req.model_dump().items()
            if k not in exporter.FORM_FIELDS and v is not None}
    try:
        res = exporter.export_tracks(
            cfg, [os.path.normpath(p) for p in req.paths], dest_root,
            subfolder=req.subfolder, codec=req.codec, quality=req.quality,
            structure=req.structure, structure_script=req.structure_script or "",
            **opts,
        )
    except ValueError as e:      # an unusable destination (inside the library…)
        raise HTTPException(400, str(e))
    if res.get("zip"):
        # The archive is fetched by id, so the client needs the URL to hand the
        # browser (a download, or a link the user can click).
        res["zip"]["url"] = f"/api/export/zip/{res['zip']['id']}"
    # A cancelled run is not a success: it copied what it copied and stopped,
    # and `ok` is what every caller reads as "the export is done".
    ok = res["failed"] == 0 and not res.get("cancelled")
    return {"ok": ok, **res}
