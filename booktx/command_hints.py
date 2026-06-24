"""Centralized CLI command-hint string builders.

Every place that prints a ``booktx translate next`` or ``booktx translate insert``
suggestion must route through this module so the defaults and flag shapes stay
in sync.  The functions accept a :class:`~booktx.config.Project` so they can
derive the ``--profile`` fragment automatically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from booktx.config import Project

__all__ = [
    "profile_option_fragment",
    "translate_next_command",
    "translate_insert_command",
    "validate_command",
    "build_command",
]

# Preferred default batch words for agent-friendly bounded runs.
# Kept as a constant so both the hint builders and the todo renderer share it.
DEFAULT_BATCH_WORDS: int = 800


def profile_option_fragment(project: Project) -> str:
    """Return `` --profile NAME`` when a profile is selected, else ``""``."""
    if project.profile:
        return f" --profile {project.profile}"
    return ""


def translate_next_command(
    project: Project,
    *,
    chapter_id: str | None = None,
    unit: str = "batch",
    max_words: int = DEFAULT_BATCH_WORDS,
    output_format: str = "block",
) -> str:
    """Build a ``booktx translate next`` command string.

    Parameters
    ----------
    project:
        The active project (used to derive ``--profile``).
    chapter_id:
        Optional chapter pin.  When omitted, booktx selects the next
        incomplete chapter automatically.
    unit:
        Translation unit (batch / paragraph / chunk / chapter).
    max_words:
        Source-word budget for the task.
    output_format:
        Output format (block / text / tsv).
    """
    chapter_part = f" --chapter {chapter_id}" if chapter_id else ""
    return (
        f"booktx translate next ."
        f"{profile_option_fragment(project)}"
        f"{chapter_part}"
        f" --unit {unit} --max-words {max_words} --format {output_format}"
    )


def translate_insert_command(
    project: Project,
    *,
    task_id: str,
    file_path: str,
    input_format: str = "block",
) -> str:
    """Build a ``booktx translate insert`` command string.

    Uses ``--json-file`` for JSON submissions (the cleaner hint) and
    ``--file ... --format block`` for block submissions.
    """
    profile_part = profile_option_fragment(project)
    if input_format == "json":
        return (
            f"booktx translate insert .{profile_part} --task-id {task_id}"
            f" --json-file {file_path}"
        )
    return (
        f"booktx translate insert .{profile_part} --task-id {task_id}"
        f" --file {file_path} --format {input_format}"
    )


def validate_command(project: Project) -> str:
    """Build a ``booktx validate`` command string."""
    return f"booktx validate .{profile_option_fragment(project)}"


def build_command(project: Project) -> str:
    """Build a ``booktx build`` command string."""
    return f"booktx build .{profile_option_fragment(project)}"
