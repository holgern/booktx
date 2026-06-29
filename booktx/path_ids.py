"""Safe artifact-id validation for profile-local path constructors.

Several booktx path constructors interpolate a user/agent-supplied id
(task id, ingest id, todo id, review-task id, review-todo id) into a profile
directory. Without validation those ids could carry path separators and escape
the profile-local directory (``../`` traversal, absolute paths).

:func:`safe_artifact_id` is the single validator. It accepts a candidate id,
checks that ``Path(value).name`` equals the original (so the id contains no
separators and no drive/anchor component), and returns it unchanged on success.
On failure it raises :class:`booktx.errors.BooktxError` with a kind-specific
``invalid_<kind>_id`` code.

All artifact path constructors in :mod:`booktx.config` route their id argument
through this helper.
"""

from __future__ import annotations

from pathlib import Path

from booktx.errors import BooktxError, _err


def safe_artifact_id(value: str, *, kind: str) -> str:
    """Validate that ``value`` is a path-safe artifact id.

    A path-safe id has no directory separators and no platform anchor: applying
    :attr:`pathlib.PurePath.name` must return the id unchanged, and it must be
    non-empty. The id is returned verbatim on success so callers can interpolate
    it into a filename without mutating it.

    Args:
        value: The candidate id (task id, todo id, review id, ...).
        kind: Short human-readable kind used in the error code and message
            (``"task"``, ``"todo"``, ``"review_todo"``, ...). The resulting
            error code is ``invalid_<kind>_id``.

    Raises:
        BooktxError: With code ``invalid_<kind>_id`` when ``value`` contains a
            separator, is absolute, is empty, or otherwise cannot be used as a
            single path component.
    """
    if not isinstance(value, str):  # defensive: typer args are str, but be safe
        raise _err(
            f"invalid_{kind}_id",
            f"Invalid {kind} id: expected a string, got {type(value).__name__}",
        )
    # Reject special path components and cross-platform separators up front.
    # ``Path('..').name`` is ``'..'`` (not empty), so the .name check alone does
    # not catch ``..``/``.``. Backslashes are separators on Windows and null
    # bytes are illegal in paths everywhere.
    if value in {".", ".."} or "\\" in value or "\x00" in value or not value:
        raise _err(f"invalid_{kind}_id", f"Invalid {kind} id: {value!r}")
    safe = Path(value).name
    if safe != value or not safe:
        raise _err(f"invalid_{kind}_id", f"Invalid {kind} id: {value!r}")
    return safe


__all__ = ["BooktxError", "safe_artifact_id"]
