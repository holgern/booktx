"""Translation-task creation, durable task paths, and submission hints.

Centralizes the durable-file layout and id derivation for translation tasks so
the command layer stops reconstructing paths and submit-hints inline. The
``TaskPaths`` value object bundles the four per-task files
(``.booktx/tasks/<id>.json``, ``.booktx/tasks/<id>.source.block.txt``,
``.booktx/ingest/<id>.json``, ``.booktx/ingest/<id>.block.txt``) and renders
the project-relative display strings and submit commands the CLI prints.
"""

from __future__ import annotations

import hashlib
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
from booktx.io_utils import write_json_text_atomic, write_text_atomic
from booktx.models import TranslationTask, TranslationTaskRecord
from booktx.versioning import resolve_current_version

if TYPE_CHECKING:
    from booktx.status import ChapterProgress, StatusBundle

__all__ = [
    "TaskPaths",
    "make_task_id",
    "task_paths",
    "project_relative",
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
        return (
            f"booktx translate insert . --task-id {task_id} "
            f"--file {project_relative(self.ingest_block, root)} --format block"
        )

    def json_submit_hint(self, task_id: str, root: Path) -> str:
        return (
            f"booktx translate insert . --task-id {task_id} "
            f"--json-file {project_relative(self.ingest_json, root)}"
        )

    def block_stdin_submit_hint(self, task_id: str) -> str:
        return (
            f"booktx translate insert . --task-id {task_id} "
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
    """Return the :class:`TaskPaths` bundle for ``task_id``."""
    return TaskPaths(
        task_json=project.tasks_dir / f"{task_id}.json",
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
    submit_hint = (
        f"booktx translate insert . --task-id {task.task_id} "
        f"--file {block_display} --format block"
    )
    headers = [
        "# booktx block submission",
        f"# task: {task.task_id}",
        f"# translation_version: {task.translation_version or 'none'}",
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


def create_translation_task(
    project: Project,
    bundle: StatusBundle,
    chapter: ChapterProgress,
    *,
    unit: str,
    record_ids: list[str],
) -> TranslationTask:
    """Build, persist, and render durable files for one translation task."""
    from booktx.config import write_translation_task

    source_by_id = bundle.index.source_by_id
    translation_version = None
    context_sha256 = None
    source_sha256 = bundle.snapshot.source.source_sha256 or None
    if bundle.snapshot.context.exists and bundle.snapshot.context.ready:
        resolution = resolve_current_version(project)
        translation_version = resolution.version_ref
        context_sha256 = resolution.context_sha256
    else:
        translation_version = load_translation_version_ledger(project).active_version
    task = TranslationTask(
        task_id=make_task_id(chapter.chapter_id, record_ids[0], record_ids),
        unit=unit,  # type: ignore[arg-type]
        chapter_id=chapter.chapter_id,
        chapter_title=chapter.title,
        source_language=project.config.source_language,
        target_language=project.config.target_language,
        translation_version=translation_version,
        context_sha256=context_sha256,
        source_sha256=source_sha256,
        source_words=sum(
            source_by_id[record_id].source_words for record_id in record_ids
        ),
        record_count=len(record_ids),
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
