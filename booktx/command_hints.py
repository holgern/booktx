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
    from booktx.runtime import RuntimeMode

__all__ = [
    "profile_option_fragment",
    "translate_next_command",
    "translate_insert_command",
    "translate_todo_status_command",
    "translate_todo_resume_command",
    "context_chapter_note_command",
    "validate_command",
    "check_command",
    "build_command",
    "review_next_command",
]

# Preferred default batch words for agent-friendly bounded runs.
# Kept as a constant so both the hint builders and the todo renderer share it.
DEFAULT_BATCH_WORDS: int = 800


def profile_option_fragment(project: Project, mode: RuntimeMode | None = None) -> str:
    """Return `` --profile NAME`` when a profile is selected, else ``""``."""
    if mode is not None and mode.isolated_output:
        return ""
    if project.profile:
        return f" --profile {project.profile}"
    return ""


def translate_next_command(
    project: Project,
    *,
    mode: RuntimeMode | None = None,
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
        f"{profile_option_fragment(project, mode)}"
        f"{chapter_part}"
        f" --unit {unit} --max-words {max_words} --format {output_format}"
    )


def translate_insert_command(
    project: Project,
    *,
    mode: RuntimeMode | None = None,
    task_id: str,
    file_path: str,
    input_format: str = "block",
) -> str:
    """Build a ``booktx translate insert`` command string.

    Uses ``--json-file`` for JSON submissions (the cleaner hint) and
    ``--file ... --format block`` for block submissions.
    """
    profile_part = profile_option_fragment(project, mode)
    if input_format == "json":
        return (
            f"booktx translate insert .{profile_part} --task-id {task_id}"
            f" --json-file {file_path}"
        )
    return (
        f"booktx translate insert .{profile_part} --task-id {task_id}"
        f" --file {file_path} --format {input_format}"
    )


def translate_todo_status_command(
    project: Project,
    *,
    mode: RuntimeMode | None = None,
    todo_id: str | None = None,
    latest: bool = False,
) -> str:
    """Build a ``booktx translate todo-status`` command string."""
    selector = f" --todo-id {todo_id}" if todo_id else " --latest" if latest else ""
    opt = profile_option_fragment(project, mode)
    return f"booktx translate todo-status .{opt}{selector}"


def translate_todo_resume_command(
    project: Project,
    *,
    mode: RuntimeMode | None = None,
    todo_id: str | None = None,
    latest: bool = False,
    output_format: str = "block",
) -> str:
    """Build a ``booktx translate todo-resume`` command string."""
    selector = f" --todo-id {todo_id}" if todo_id else " --latest" if latest else ""
    return (
        f"booktx translate todo-resume ."
        f"{profile_option_fragment(project, mode)}{selector} --format {output_format}"
    )


def context_chapter_note_command(
    project: Project,
    *,
    mode: RuntimeMode | None = None,
    chapter_id: str,
    title: str = "<TITLE>",
    source_summary: str = "<SOURCE_SUMMARY>",
    translation_summary: str = "<TRANSLATION_SUMMARY>",
    decision: str = "<DECISION>",
) -> str:
    """Build a template ``booktx context chapter-note`` command string."""
    return (
        f"booktx context chapter-note .{profile_option_fragment(project, mode)}"
        f" {chapter_id}"
        f' --title "{title}"'
        f' --source-summary "{source_summary}"'
        f' --translation-summary "{translation_summary}"'
        f' --decision "{decision}"'
    )


def validate_command(
    project: Project,
    *,
    mode: RuntimeMode | None = None,
    fail_on_warnings: bool = False,
) -> str:
    """Build a ``booktx validate`` command string."""
    strict = " --fail-on-warnings" if fail_on_warnings else ""
    return f"booktx validate .{profile_option_fragment(project, mode)}{strict}"


def check_command(
    project: Project | None,
    *,
    mode: RuntimeMode | None = None,
    chapter_id: str | None = None,
    task_id: str | None = None,
    fail_on_warnings: bool = True,
) -> str:
    """Build a ``booktx check`` command string.

    ``check`` is the human-friendly alias for scoped validation + EPUB inline
    preflight. ``project`` may be ``None`` when the hint is rendered without a
    resolved project (e.g. inside build errors); in that case the profile and
    path fragments are omitted.
    """
    profile = profile_option_fragment(project, mode) if project is not None else ""
    chapter = f" --chapter {chapter_id}" if chapter_id else ""
    task = f" --task-id {task_id}" if task_id else ""
    strict = " --fail-on-warnings" if fail_on_warnings else ""
    return f"booktx check .{profile}{chapter}{task}{strict}"


def build_command(
    project: Project,
    *,
    mode: RuntimeMode | None = None,
    require_complete: bool = False,
) -> str:
    """Build a ``booktx build`` command string."""
    strict = " --require-complete" if require_complete else ""
    return f"booktx build .{profile_option_fragment(project, mode)}{strict}"


def review_next_command(
    project: Project,
    *,
    mode: RuntimeMode | None = None,
    pass_number: int,
    chapter_id: str | None = None,
    max_words: int = 900,
    selection: str = "missing",
    base: str | None = None,
) -> str:
    """Build a ``booktx review next`` command string for a pass.

    ``selection``/``base`` are only emitted when they differ from the defaults
    (``missing`` / pass-config-derived), so routine hints stay short.
    """
    profile = profile_option_fragment(project, mode)
    chapter = f" --chapter {chapter_id}" if chapter_id else ""
    parts = [
        f"booktx review next .{profile} --pass {pass_number}{chapter}"
        f" --max-words {max_words}"
    ]
    if selection != "missing":
        parts.append(f" --selection {selection}")
    if base is not None:
        parts.append(f" --base {base}")
    return "".join(parts)
