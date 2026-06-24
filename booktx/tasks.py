"""Translation-task creation, durable task paths, and submission hints.

Centralizes the durable-file layout and id derivation for translation tasks
so the command layer stops reconstructing paths and submit-hints inline.

Profile-layout projects (primary):

    translations/<profile>/tasks/<id>.json
    translations/<profile>/tasks/<id>.source.block.txt
    translations/<profile>/ingest/<id>.json
    translations/<profile>/ingest/<id>.block.txt

Legacy single-layout projects (compatibility only):

    .booktx/tasks/<id>.json
    .booktx/tasks/<id>.source.block.txt
    .booktx/ingest/<id>.json
    .booktx/ingest/<id>.block.txt

All task-path access goes through ``translation_task_dir(project)`` which
enforces the profile-required guard for source-only projects. The
``TaskPaths`` value object bundles the four per-task files and renders the
project-relative display strings and submit commands the CLI prints.
"""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from booktx.config import (
    Project,
    load_translation_version_ledger,
    translation_ingest_block_path,
    translation_ingest_path,
    translation_task_source_block_path,
)
from booktx.context import ensure_context_view_snapshot
from booktx.io_utils import write_json_text_atomic, write_text_atomic
from booktx.models import TranslationTask, TranslationTaskRecord
from booktx.versioning import canonical_json_sha256, resolve_current_version

if TYPE_CHECKING:
    from booktx.progress import SourceRecordView
    from booktx.status import ChapterProgress, StatusBundle

__all__ = [
    "TaskPaths",
    "make_task_id",
    "task_paths",
    "project_relative",
    "limit_records_by_words",
    "select_translation_record_ids",
    "create_translation_task",
    "write_ingest_template",
    "write_block_ingest_template",
    "write_task_source_block",
]


def project_relative(path: Path, root: Path) -> str:
    """Return a stable project-relative display path when possible."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def make_task_id(chapter_id: str, first_record_id: str, record_ids: list[str]) -> str:
    """Derive a deterministic, path-safe task id.

    Uses a stable ``blake2s`` digest (``digest_size=4``) of the joined record
    ids instead of Python's process-randomized ``hash()``, plus a
    seconds-precision UTC timestamp so same-day collisions are extremely
    unlikely.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record_part = first_record_id.replace("-", "")
    digest = hashlib.blake2s(
        "|".join(record_ids).encode("utf-8"), digest_size=4
    ).hexdigest()
    return f"bt-task-{stamp}-{chapter_id}-{record_part}-{digest}"


@dataclass(frozen=True, slots=True)
class TaskPaths:
    """The four durable files owned by one translation task."""

    task_json: Path
    source_block: Path
    ingest_json: Path
    ingest_block: Path

    def display(self, root: Path) -> TaskPathDisplay:
        return TaskPathDisplay(
            task_json=project_relative(self.task_json, root),
            source_block=project_relative(self.source_block, root),
            ingest_json=project_relative(self.ingest_json, root),
            ingest_block=project_relative(self.ingest_block, root),
        )

    def block_submit_hint(self, task_id: str, root: Path) -> str:
        profile_part = (
            f" --profile {self.task_json.parent.parent.name}"
            if self.task_json.parent.parent.name != ".booktx"
            else ""
        )
        return (
            f"booktx translate insert .{profile_part} --task-id {task_id} "
            f"--file {project_relative(self.ingest_block, root)} --format block"
        )

    def json_submit_hint(self, task_id: str, root: Path) -> str:
        profile_part = (
            f" --profile {self.task_json.parent.parent.name}"
            if self.task_json.parent.parent.name != ".booktx"
            else ""
        )
        return (
            f"booktx translate insert .{profile_part} --task-id {task_id} "
            f"--json-file {project_relative(self.ingest_json, root)}"
        )

    def block_stdin_submit_hint(self, task_id: str) -> str:
        profile_part = (
            f" --profile {self.task_json.parent.parent.name}"
            if self.task_json.parent.parent.name != ".booktx"
            else ""
        )
        return (
            f"booktx translate insert .{profile_part} --task-id {task_id} "
            "--stdin --format block <<'BOOKTX'"
        )


@dataclass(frozen=True, slots=True)
class TaskPathDisplay:
    """Project-relative display strings for a task's durable files."""

    task_json: str
    source_block: str
    ingest_json: str
    ingest_block: str


def task_paths(project: Project, task_id: str) -> TaskPaths:
    """Return the :class:`TaskPaths` bundle for ``task_id``.

    Routes through ``translation_task_dir(project)`` so a source-only
    project (no selected profile) hits the profile-required guard instead of
    silently assuming legacy ``.booktx/tasks`` paths.
    """
    from booktx.config import translation_task_dir

    return TaskPaths(
        task_json=translation_task_dir(project) / f"{task_id}.json",
        source_block=translation_task_source_block_path(project, task_id),
        ingest_json=translation_ingest_path(project, task_id),
        ingest_block=translation_ingest_block_path(project, task_id),
    )


def write_ingest_template(project: Project, task: TranslationTask) -> Path:
    """Create the durable JSON submission file for a task without overwriting work."""
    path = translation_ingest_path(project, task.task_id)
    if path.exists():
        return path
    payload = {
        "schema_version": 2,
        "profile": task.profile or None,
        "task_id": task.task_id,
        "translation_version": task.translation_version,
        "records": [{"id": record.id, "target": ""} for record in task.records],
    }
    import json

    write_json_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False))
    return path


def write_block_ingest_template(project: Project, task: TranslationTask) -> Path:
    """Create the durable block submission file for a task without overwriting work.

    The file starts with metadata comment headers (ignored by the block parser)
    followed by one ``>>> RECORD_ID`` header per record. The agent fills in the
    target text under each header.
    """
    path = translation_ingest_block_path(project, task.task_id)
    if path.exists():
        return path
    paths = task_paths(project, task.task_id)
    source_display = project_relative(paths.source_block, project.root)
    block_display = project_relative(path, project.root)
    from booktx.command_hints import translate_insert_command

    submit_hint = translate_insert_command(
        project, task_id=task.task_id, file_path=block_display
    )
    context_display_path = (
        task.context_view_path.replace("context.json", "context.md")
        if task.context_view_path
        else ""
    )
    headers = [
        "# booktx block submission",
        f"# profile: {task.profile or 'none'}",
        f"# target: {task.target_locale or task.target_language}",
        f"# task: {task.task_id}",
        f"# translation_version: {task.translation_version or 'none'}",
        f"# baseline: {task.baseline_ref or task.translation_version or 'none'}",
        f"# baseline_sha256: {task.baseline_sha256 or ''}",
        f"# context_sha256: {task.context_sha256 or ''}",
        f"# context_view_sha256: {task.context_view_sha256 or ''}",
        f"# context_notes_scope: {task.context_notes_scope or ''}",
        f"# context_target_chapter_id: {task.context_target_chapter_id or ''}",
        f"# context_notes_through_chapter_id: {task.context_notes_through_chapter_id or ''}",
        f"# context_view_path: {task.context_view_path or ''}",
        f"# context_file: {context_display_path}",
        f"# source_sha256: {task.source_sha256 or ''}",
        f"# source: {source_display}",
        f"# submit: {submit_hint}",
        "",
    ]
    parts = [f">>> {record.id}" for record in task.records]
    write_text_atomic(path, "\n".join(headers + parts).rstrip() + "\n")
    return path


def write_task_source_block(project: Project, task: TranslationTask) -> Path:
    """Create the durable source-view file for a task without overwriting work.

    Holds the original source text for each record in the task so a coding
    agent can translate against a stable file instead of a large stdout dump.
    """
    path = translation_task_source_block_path(project, task.task_id)
    if path.exists():
        return path
    parts = [
        f"# profile: {task.profile or 'none'}",
        f"# target: {task.target_locale or task.target_language}",
        f"# task: {task.task_id}",
        f"# chapter: {task.chapter_id} {task.chapter_title}".rstrip(),
        f"# unit: {task.unit}",
        f"# records: {task.record_count}",
        f"# source words: {task.source_words}",
        "",
    ]
    for idx, record in enumerate(task.records):
        if idx:
            parts.append("")
        parts.append(f">>> {record.id}")
        parts.append(record.source)
    write_text_atomic(path, "\n".join(parts).rstrip() + "\n")
    return path


def limit_records_by_words(
    record_ids: list[str],
    source_by_id: Mapping[str, SourceRecordView],
    max_words: int,
) -> list[str]:
    """Return the longest prefix of ``record_ids`` within ``max_words``.

    The first record is always included when ``record_ids`` is non-empty so a
    single long record still makes progress.
    """
    if max_words < 1:
        raise ValueError("max_words must be >= 1")
    selected: list[str] = []
    total = 0
    for record_id in record_ids:
        words = source_by_id[record_id].source_words
        if selected and total + words > max_words:
            break
        selected.append(record_id)
        total += words
    return selected


def select_translation_record_ids(
    bundle: StatusBundle,
    chapter: ChapterProgress,
    *,
    unit: str,
    max_words: int,
) -> tuple[str, list[str]]:
    """Select the record ids for the next translation task within ``chapter``."""
    source_by_id = bundle.index.source_by_id
    pending = [
        record_id
        for record_id in bundle.index.record_ids_by_chapter[chapter.chapter_id]
        if record_id not in bundle.index.translated_by_id
    ]
    if not pending:
        return (unit, [])
    if unit == "chapter":
        return (unit, pending)
    if unit == "chunk":
        first_chunk_id = source_by_id[pending[0]].chunk_id
        return (
            unit,
            [
                record_id
                for record_id in pending
                if source_by_id[record_id].chunk_id == first_chunk_id
            ],
        )
    if unit == "paragraph":
        first_record = source_by_id[pending[0]]
        if first_record.span_index is None:
            unit = "batch"
        else:
            same_span = [
                record_id
                for record_id in pending
                if source_by_id[record_id].span_index == first_record.span_index
            ]
            return (unit, limit_records_by_words(same_span, source_by_id, max_words))
    return (unit, limit_records_by_words(pending, source_by_id, max_words))


def create_translation_task(
    project: Project,
    bundle: StatusBundle,
    chapter: ChapterProgress,
    *,
    unit: str,
    record_ids: list[str],
    requested_max_words: int | None = None,
    todo_id: str | None = None,
) -> TranslationTask:
    """Build, persist, and render durable files for one translation task."""
    from booktx.config import write_translation_task

    source_by_id = bundle.index.source_by_id
    translation_version = None
    baseline_ref = None
    baseline_sha = None
    context_sha256 = None
    context_view_sha256 = None
    context_view_path = None
    context_notes_scope = None
    context_target_chapter_id = None
    context_notes_through_chapter_id = None
    source_sha256 = bundle.snapshot.source.source_sha256 or None
    if bundle.snapshot.context.exists and bundle.snapshot.context.ready:
        resolution = resolve_current_version(project)
        context_view = ensure_context_view_snapshot(
            project,
            baseline_ref=resolution.version_ref,
            baseline_sha256=resolution.baseline_sha256,
            target_chapter_id=chapter.chapter_id,
        )
        translation_version = resolution.version_ref
        baseline_ref = resolution.version_ref
        baseline_sha = resolution.baseline_sha256
        context_sha256 = context_view.context_view_sha256
        context_view_sha256 = context_view.context_view_sha256
        context_view_path = context_view.context_path
        context_notes_scope = context_view.notes_scope
        context_target_chapter_id = context_view.target_chapter_id
        context_notes_through_chapter_id = context_view.notes_through_chapter_id
    else:
        translation_version = load_translation_version_ledger(project).active_version
    task = TranslationTask(
        task_id=make_task_id(chapter.chapter_id, record_ids[0], record_ids),
        unit=unit,  # type: ignore[arg-type]
        chapter_id=chapter.chapter_id,
        chapter_title=chapter.title,
        profile=project.profile or "",
        source_language=project.config.source_language,
        target_language=project.config.target_language,
        target_locale=project.config.target_locale or project.config.target_language,
        translation_version=translation_version,
        baseline_ref=baseline_ref,
        baseline_sha256=baseline_sha,
        context_sha256=context_sha256,
        context_view_sha256=context_view_sha256,
        context_view_path=context_view_path,
        context_notes_scope=context_notes_scope,
        context_target_chapter_id=context_target_chapter_id,
        context_notes_through_chapter_id=context_notes_through_chapter_id,
        source_sha256=source_sha256,
        profile_config_sha256=(
            canonical_json_sha256(project.profile_config.model_dump(mode="json"))
            if project.profile_config is not None
            else None
        ),
        source_config_sha256=canonical_json_sha256(
            project.source_config.model_dump(mode="json")
        ),
        source_words=sum(
            source_by_id[record_id].source_words for record_id in record_ids
        ),
        record_count=len(record_ids),
        requested_max_words=requested_max_words,
        todo_id=todo_id,
        records=[
            TranslationTaskRecord(
                id=record_id,
                chunk_id=source_by_id[record_id].chunk_id,
                source=source_by_id[record_id].source,
                protected_terms=list(source_by_id[record_id].protected_terms),
                placeholders=list(source_by_id[record_id].placeholders),
            )
            for record_id in record_ids
        ],
    )
    write_translation_task(project, task)
    write_ingest_template(project, task)
    write_block_ingest_template(project, task)
    write_task_source_block(project, task)
    return task
