"""Bounded translation todo loading, progress, and status snapshots."""

from __future__ import annotations

from dataclasses import dataclass

from booktx.command_hints import (
    translate_todo_resume_command,
    validate_command,
)
from booktx.config import (
    Project,
    _err,
    translation_todo_dir,
    translation_todo_json_path,
)
from booktx.models import TranslationTodo
from booktx.status import StatusBundle
from booktx.validate import ValidationReport
from booktx.versioning import resolve_current_version

__all__ = [
    "TodoChapterStatus",
    "TodoValidationStatus",
    "TodoStatusSnapshot",
    "load_translation_todo",
    "list_translation_todos",
    "latest_incomplete_todo",
    "build_todo_status",
]


@dataclass(slots=True)
class TodoChapterStatus:
    chapter_id: str
    title: str
    status_at_start: str
    status_now: str
    records_total: int
    records_translated_at_start: int
    records_translated_now: int
    records_remaining_now: int
    source_words_remaining_at_start: int
    source_words_remaining_now: int
    complete: bool
    missing: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "chapter_id": self.chapter_id,
            "title": self.title,
            "status_at_start": self.status_at_start,
            "status_now": self.status_now,
            "records_total": self.records_total,
            "records_translated_at_start": self.records_translated_at_start,
            "records_translated_now": self.records_translated_now,
            "records_remaining_now": self.records_remaining_now,
            "source_words_remaining_at_start": self.source_words_remaining_at_start,
            "source_words_remaining_now": self.source_words_remaining_now,
            "complete": self.complete,
            "missing": self.missing,
        }


@dataclass(slots=True)
class TodoValidationStatus:
    errors: int
    warnings: int
    blocking: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "errors": self.errors,
            "warnings": self.warnings,
            "blocking": self.blocking,
        }


@dataclass(slots=True)
class TodoStatusSnapshot:
    todo: TranslationTodo
    chapters: list[TodoChapterStatus]
    complete_count: int
    goal_complete: bool
    current_chapter: TodoChapterStatus | None
    next_planned_chapter: str | None
    source_drifted: bool
    context_drifted: bool
    validation: TodoValidationStatus
    state: str
    blocking_reason: str | None
    next_safe_command: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "todo_id": self.todo.todo_id,
            "profile": self.todo.profile,
            "target_language": self.todo.target_language,
            "target_locale": self.todo.target_locale,
            "created_at": self.todo.created_at,
            "baseline_ref": self.todo.baseline_ref,
            "baseline_sha256": self.todo.baseline_sha256,
            "chapters_requested": self.todo.chapters_requested,
            "batch_words": self.todo.batch_words,
            "max_run_words": self.todo.max_run_words,
            "goal_complete": self.goal_complete,
            "chapters_complete": self.complete_count,
            "chapters_total": len(self.chapters),
            "source_drifted": self.source_drifted,
            "context_drifted": self.context_drifted,
            "validation": self.validation.as_dict(),
            "state": self.state,
            "blocking_reason": self.blocking_reason,
            "next_planned_chapter": self.next_planned_chapter,
            "next_safe_command": self.next_safe_command,
            "chapters": [chapter.as_dict() for chapter in self.chapters],
        }


def load_translation_todo(project: Project, todo_id: str) -> TranslationTodo | None:
    """Load one durable bounded-run todo by id."""
    path = translation_todo_json_path(project, todo_id)
    if not path.is_file():
        return None
    try:
        return TranslationTodo.model_validate_json(path.read_text("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise _err("invalid_todo", f"todo {todo_id} is invalid: {exc}") from exc


def list_translation_todos(project: Project) -> list[TranslationTodo]:
    """Return all durable bounded-run todos sorted by creation time."""
    todo_dir = translation_todo_dir(project)
    if not todo_dir.exists():
        return []
    todos: list[TranslationTodo] = []
    for path in sorted(todo_dir.glob("*.json")):
        try:
            todos.append(TranslationTodo.model_validate_json(path.read_text("utf-8")))
        except Exception as exc:  # noqa: BLE001
            raise _err(
                "invalid_todo",
                f"todo file {path.name} is invalid: {exc}",
            ) from exc
    todos.sort(key=lambda todo: (todo.created_at, todo.todo_id))
    return todos


def _recreate_todo_command(project: Project, todo: TranslationTodo) -> str:
    return (
        f"booktx translate todo-next . --profile {todo.profile}"
        f" --chapters {todo.chapters_requested}"
        f" --batch-words {todo.batch_words} --write"
    )


def _current_context_sha256(project: Project, bundle: StatusBundle) -> str | None:
    if not bundle.snapshot.context.exists or not bundle.snapshot.context.ready:
        return None
    resolution = resolve_current_version(project)
    return resolution.baseline_sha256


def _chapter_statuses(
    todo: TranslationTodo, bundle: StatusBundle
) -> tuple[list[TodoChapterStatus], TodoChapterStatus | None]:
    chapters: list[TodoChapterStatus] = []
    current: TodoChapterStatus | None = None
    for planned in todo.chapters:
        live = bundle.index.chapters_by_id.get(planned.chapter_id)
        if live is None:
            chapter = TodoChapterStatus(
                chapter_id=planned.chapter_id,
                title=planned.title,
                status_at_start=planned.status,
                status_now="missing",
                records_total=planned.records_total,
                records_translated_at_start=planned.records_translated_at_start,
                records_translated_now=0,
                records_remaining_now=planned.records_total,
                source_words_remaining_at_start=planned.source_words_remaining_at_start,
                source_words_remaining_now=planned.source_words_remaining_at_start,
                complete=False,
                missing=True,
            )
        else:
            chapter = TodoChapterStatus(
                chapter_id=planned.chapter_id,
                title=planned.title or live.title,
                status_at_start=planned.status,
                status_now=live.status,
                records_total=live.records_total,
                records_translated_at_start=planned.records_translated_at_start,
                records_translated_now=live.records_translated,
                records_remaining_now=live.records_remaining,
                source_words_remaining_at_start=planned.source_words_remaining_at_start,
                source_words_remaining_now=live.source_words_remaining,
                complete=live.records_remaining == 0,
            )
        if current is None and not chapter.complete:
            current = chapter
        chapters.append(chapter)
    return chapters, current


def build_todo_status(
    project: Project,
    todo: TranslationTodo,
    bundle: StatusBundle,
    *,
    validation_report: ValidationReport | None = None,
    fail_on_warnings: bool = True,
) -> TodoStatusSnapshot:
    """Build the live status snapshot for one bounded-run todo."""
    chapters, current = _chapter_statuses(todo, bundle)
    complete_count = sum(1 for chapter in chapters if chapter.complete)
    goal_complete = complete_count == len(chapters)
    source_drifted = bundle.snapshot.source.source_drifted
    if todo.source_sha256 is not None:
        source_drifted = (
            source_drifted or bundle.snapshot.source.source_sha256 != todo.source_sha256
        )
    current_context_sha = _current_context_sha256(project, bundle)
    expected_context_sha = todo.baseline_sha256 or todo.context_sha256
    context_drifted = (
        expected_context_sha is not None and current_context_sha != expected_context_sha
    )
    report = validation_report
    validation = TodoValidationStatus(
        errors=len(report.errors) if report is not None else 0,
        warnings=len(report.warnings) if report is not None else 0,
        blocking=bool(
            report is not None
            and (report.errors or (fail_on_warnings and report.warnings))
        ),
    )
    next_planned_chapter = current.chapter_id if current is not None else None

    state = "ready"
    blocking_reason: str | None = None
    next_safe_command: str | None = None
    if goal_complete:
        state = "complete"
    elif source_drifted:
        state = "blocked"
        blocking_reason = "source drifted since the todo was created"
        next_safe_command = "booktx extract ."
    elif context_drifted:
        state = "blocked"
        blocking_reason = "context changed since the todo was created"
        next_safe_command = _recreate_todo_command(project, todo)
    elif validation.blocking:
        state = "blocked"
        blocking_reason = "validation findings must be resolved before resuming"
        next_safe_command = validate_command(project, fail_on_warnings=True)
    elif current is not None:
        next_safe_command = translate_todo_resume_command(
            project,
            todo_id=todo.todo_id,
            output_format="block",
        )

    return TodoStatusSnapshot(
        todo=todo,
        chapters=chapters,
        complete_count=complete_count,
        goal_complete=goal_complete,
        current_chapter=current,
        next_planned_chapter=next_planned_chapter,
        source_drifted=source_drifted,
        context_drifted=context_drifted,
        validation=validation,
        state=state,
        blocking_reason=blocking_reason,
        next_safe_command=next_safe_command,
    )


def latest_incomplete_todo(
    project: Project, bundle: StatusBundle
) -> TranslationTodo | None:
    """Return the latest incomplete bounded-run todo when the choice is safe."""
    todos = list_translation_todos(project)
    incomplete: list[TranslationTodo] = []
    chapter_sets: dict[str, set[str]] = {}
    for todo in todos:
        status = build_todo_status(project, todo, bundle, fail_on_warnings=False)
        if not status.goal_complete:
            incomplete.append(todo)
            chapter_sets[todo.todo_id] = {
                chapter.chapter_id for chapter in todo.chapters
            }
    if not incomplete:
        return None
    latest = max(incomplete, key=lambda todo: (todo.created_at, todo.todo_id))
    latest_chapters = chapter_sets[latest.todo_id]
    overlaps = [
        todo.todo_id
        for todo in incomplete
        if todo.todo_id != latest.todo_id
        and latest_chapters.intersection(chapter_sets[todo.todo_id])
    ]
    if overlaps:
        overlap_display = ", ".join(sorted(overlaps))
        raise _err(
            "ambiguous_latest_todo",
            (
                f"latest incomplete todo {latest.todo_id} overlaps planned chapters "
                f"with {overlap_display}. Use --todo-id to select the intended todo."
            ),
        )
    return latest
