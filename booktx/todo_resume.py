"""Resumable bounded-run translation task creation."""

from __future__ import annotations

from booktx.command_hints import (
    translate_todo_resume_command,
    validate_command,
)
from booktx.config import Project, _err
from booktx.models import TranslationTask, TranslationTodo
from booktx.status import StatusBundle
from booktx.tasks import create_translation_task, select_translation_record_ids
from booktx.todo_status import (
    build_todo_status,
    latest_incomplete_todo,
    load_translation_todo,
)
from booktx.validate import validate_project

__all__ = [
    "resolve_translation_todo",
    "resume_translation_todo",
]


def resolve_translation_todo(
    project: Project,
    bundle: StatusBundle,
    *,
    todo_id: str | None = None,
    latest: bool = False,
) -> TranslationTodo:
    """Resolve a bounded-run todo from ``--todo-id`` or ``--latest``."""
    if bool(todo_id) == bool(latest):
        raise _err(
            "todo_selector_required",
            "pass exactly one of --todo-id or --latest",
        )
    if todo_id is not None:
        todo = load_translation_todo(project, todo_id)
        if todo is None:
            raise _err("unknown_todo", f"unknown todo id: {todo_id}")
        return todo
    todo = latest_incomplete_todo(project, bundle)
    if todo is None:
        raise _err("no_incomplete_todo", "no incomplete translation todo was found")
    return todo


def resume_translation_todo(
    project: Project,
    bundle: StatusBundle,
    *,
    todo_id: str | None = None,
    latest: bool = False,
) -> TranslationTask:
    """Create the next bounded translation task pinned to the todo's chapter set."""
    todo = resolve_translation_todo(project, bundle, todo_id=todo_id, latest=latest)
    report = validate_project(project)
    status = build_todo_status(
        project,
        todo,
        bundle,
        validation_report=report,
        fail_on_warnings=True,
    )
    if status.goal_complete:
        raise _err(
            "todo_complete",
            f"todo {todo.todo_id} is already complete. No further task will be issued.",
        )
    if status.source_drifted:
        raise _err(
            "todo_source_drift",
            (
                f"todo {todo.todo_id} cannot resume because the source changed. "
                "Run `booktx extract .` and create a fresh todo."
            ),
        )
    if status.context_drifted:
        raise _err(
            "todo_context_drift",
            (
                f"todo {todo.todo_id} cannot resume because the context changed. "
                "Create a fresh bounded todo before requesting more work."
            ),
        )
    if report.errors or report.warnings:
        strict_validate = validate_command(project, fail_on_warnings=True)
        raise _err(
            "todo_validation_blocked",
            (
                f"todo {todo.todo_id} cannot resume because "
                f"{strict_validate} reports {len(report.errors)} error(s) "
                f"and {len(report.warnings)} warning(s)."
            ),
        )
    current = status.current_chapter
    if current is None:
        raise _err(
            "todo_complete",
            f"todo {todo.todo_id} is already complete. No further task will be issued.",
        )
    selected_chapter = bundle.index.chapters_by_id.get(current.chapter_id)
    if selected_chapter is None:
        raise _err(
            "todo_chapter_missing",
            (
                f"todo {todo.todo_id} cannot resume because planned chapter "
                f"{current.chapter_id} is no longer present."
            ),
        )
    actual_unit, record_ids = select_translation_record_ids(
        bundle,
        selected_chapter,
        unit="batch",
        max_words=todo.batch_words,
    )
    if not record_ids:
        resume_command = translate_todo_resume_command(project, todo_id=todo.todo_id)
        raise _err(
            "todo_no_remaining_records",
            (
                f"todo {todo.todo_id} expected remaining records in chapter "
                f"{selected_chapter.chapter_id}, but none were available. "
                f"Review the todo with `{resume_command}`."
            ),
        )
    return create_translation_task(
        project,
        bundle,
        selected_chapter,
        unit=actual_unit,
        record_ids=record_ids,
        requested_max_words=todo.batch_words,
        todo_id=todo.todo_id,
    )
