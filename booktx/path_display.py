"""Path display and redaction helpers for runtime-aware CLI output."""

from __future__ import annotations

from pathlib import Path

from booktx.runtime import RuntimeMode

__all__ = [
    "display_path",
    "display_source_ref",
]


def _relative_or_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def display_source_ref(mode: RuntimeMode) -> str:
    if mode.isolated_output:
        return "source records"
    return ".booktx/chunks"


def display_path(path: Path, mode: RuntimeMode) -> str:
    """Render ``path`` for the current runtime mode without leaking parents."""
    resolved = path.expanduser().resolve()
    if not mode.isolated_output:
        return _relative_or_posix(resolved, mode.project_root)

    profile_root = mode.profile_root
    if profile_root is not None:
        try:
            return resolved.relative_to(profile_root).as_posix() or "."
        except ValueError:
            pass

    project_booktx = mode.project_root / ".booktx"
    project_source = mode.project_root / "source"
    translations_root = mode.project_root / "translations"

    try:
        resolved.relative_to(project_booktx / "chunks")
        return display_source_ref(mode)
    except ValueError:
        pass
    try:
        resolved.relative_to(project_booktx)
        return "<shared source state>"
    except ValueError:
        pass
    try:
        resolved.relative_to(project_source)
        return "<source file>"
    except ValueError:
        pass
    try:
        rel = resolved.relative_to(translations_root)
        if rel.parts and rel.parts[0] != (mode.profile_name or ""):
            return "<hidden>"
    except ValueError:
        pass
    return "<hidden>"
