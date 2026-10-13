"""Compatibility re-export — the naming-script evaluator lives in the engine
(mlo.naming) so the grader can verify file paths against the script without
importing from the server layer."""

from mlo.naming import (  # noqa: F401
    DEFAULT_NAMING_SCRIPT,
    eval_script,
    sanitize_path,
    shared_album_tags,
    track_variables,
)
