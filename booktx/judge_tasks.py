"""Judge-task creation and durable artifact rendering."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import blake2s
from typing import TYPE_CHECKING

from booktx.config import (
    current_source_sha256,
    judge_ingest_block_path,
    judge_ingest_json_path,
    judge_task_source_block_path,
    write_judge_task,
)
from booktx.context import ensure_context_view_snapshot, load_context
from booktx.glossary_match import live_mandatory_glossary_sha256
from booktx.io_utils import write_text_atomic
from booktx.judge_store import (
    collect_source_candidates,
    load_source_profile_projects,
    require_selection_profile,
    selected_record_ids,
)
from booktx.models import JudgeTask, JudgeTaskRecord
from booktx.progress import count_words
from booktx.record_refs import parse_record_ref
from booktx.status import selected_chapter
from booktx.tasks import limit_records_by_words
from booktx.validate import load_validation_context
from booktx.versioning import canonical_json_sha256, resolve_current_version

if TYPE_CHECKING:
    from booktx.config import Project
    from booktx.status import StatusBundle

__all__ = [
    "make_judge_task_id",
    "create_judge_task",
    "render_judge_task_block",
    "render_judge_ingest_json",
]


def make_judge_task_id(
    chapter_id: str, first_record_id: str, record_ids: list[str]
) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = blake2s("|".join(record_ids).encode("utf-8"), digest_size=4).hexdigest()
    return f"bt-judge-{stamp}-{chapter_id}-{first_record_id.replace('-', '')}-{digest}"


def _record_ids_for_task(
    *,
    project: Project,
    bundle: StatusBundle,
    chapter_id: str | None,
    record_id: str | None,
    max_words: int,
) -> tuple[str, list[str]]:
    if record_id is not None:
        canonical = parse_record_ref(record_id).canonical_id
        chapter = bundle.index.record_to_chapter.get(canonical)
        if chapter is None:
            raise ValueError(f"unknown source record id: {canonical}")
        return chapter, [canonical]

    chapter_obj = selected_chapter(bundle, chapter_id)
    if chapter_obj is None:
        raise ValueError("no eligible chapter found")
    selected_ids = [
        rid
        for rid in bundle.index.record_ids_by_chapter.get(chapter_obj.chapter_id, [])
        if rid not in selected_record_ids(project)
    ]
    return (
        chapter_obj.chapter_id,
        limit_records_by_words(selected_ids, bundle.index.source_by_id, max_words),
    )


def render_judge_task_block(task: JudgeTask) -> str:
    lines = [
        "# booktx judge task",
        f"judge_task_id: {task.judge_task_id}",
        f"profile: {task.profile}",
        f"sources: {','.join(task.source_profiles)}",
        "",
    ]
    for record in task.records:
        lines.extend(
            [
                f"## {record.id}",
                "",
                "SOURCE:",
                record.source,
                "",
                "CANDIDATES:",
                "",
            ]
        )
        for candidate in record.candidates:
            lines.append(
                f"[{candidate.label}] profile={candidate.profile} "
                f"ref={candidate.selected_ref} sha256={candidate.target_sha256}"
            )
            lines.append(candidate.target)
            if candidate.validation_findings:
                messages = "; ".join(
                    f"{finding.severity}:{finding.rule}"
                    for finding in candidate.validation_findings
                )
                lines.append(f"validation: {messages}")
            lines.append("")
        if record.missing_profiles:
            lines.append("missing_profiles: " + ", ".join(record.missing_profiles))
            lines.append("")
        lines.extend(
            [
                "DECISION:",
                "selected: ",
                "decision_kind: copy",
                "reason: ",
                "",
                "TARGET:",
                "",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_judge_ingest_json(task: JudgeTask) -> str:
    payload = {
        "judge_task_id": task.judge_task_id,
        "records": [
            {
                "id": record.id,
                "selected": "",
                "decision_kind": "copy",
                "target": "",
                "reason": "",
            }
            for record in task.records
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def create_judge_task(
    project: Project,
    bundle: StatusBundle,
    *,
    source_profiles: list[str],
    chapter_id: str | None,
    record_id: str | None,
    max_words: int,
    require_all_sources: bool,
) -> JudgeTask:
    require_selection_profile(project)
    context_exists = load_context(project) is not None
    if not context_exists:
        raise ValueError("selection profile context is missing")

    task_chapter_id, selected_ids = _record_ids_for_task(
        project=project,
        bundle=bundle,
        chapter_id=chapter_id,
        record_id=record_id,
        max_words=max_words,
    )
    if not selected_ids:
        raise ValueError("no missing records remain for the requested chapter")

    resolution = resolve_current_version(project)
    context_view = ensure_context_view_snapshot(
        project,
        baseline_ref=resolution.version_ref,
        baseline_sha256=resolution.baseline_sha256,
        target_chapter_id=task_chapter_id,
    )
    source_projects = load_source_profile_projects(project, source_profiles)
    validation_context = load_validation_context(
        project,
        context_view_path=context_view.context_path,
    )

    records: list[JudgeTaskRecord] = []
    total_words = 0
    for record_ref in selected_ids:
        source_view = bundle.index.source_by_id[record_ref]
        source_chunk = bundle.index.source_chunks[source_view.chunk_id]
        source_record = next(
            item for item in source_chunk.records if item.id == record_ref
        )
        candidates, missing_profiles = collect_source_candidates(
            selection_project=project,
            selection_context=validation_context,
            source_projects=source_projects,
            source_record=source_record,
            chunk_id=source_view.chunk_id,
        )
        if require_all_sources and missing_profiles:
            raise ValueError(
                f"record {record_ref} is missing effective candidates for: "
                f"{', '.join(missing_profiles)}",
            )
        if not candidates:
            continue
        next_words = total_words + count_words(source_record.source)
        if records and next_words > max_words:
            break
        total_words = next_words
        records.append(
            JudgeTaskRecord(
                id=record_ref,
                chunk_id=source_view.chunk_id,
                source=source_record.source,
                source_sha256=source_view.source_sha256,
                candidates=candidates,
                missing_profiles=missing_profiles,
                output_version_ref=resolution.version_ref,
            )
        )
    if not records:
        raise ValueError("no judgeable records found for the requested scope")

    chapter = bundle.index.chapters_by_id[task_chapter_id]
    task = JudgeTask(
        judge_task_id=make_judge_task_id(
            task_chapter_id, records[0].id, [r.id for r in records]
        ),
        profile=project.profile or "",
        source_profiles=list(source_profiles),
        source_language=project.config.source_language,
        target_language=project.config.target_language,
        target_locale=project.config.target_locale or project.config.target_language,
        chapter_id=task_chapter_id,
        chapter_title=chapter.title,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        source_sha256=current_source_sha256(project),
        profile_config_sha256=(
            canonical_json_sha256(project.profile_config.model_dump(mode="json"))
            if project.profile_config is not None
            else None
        ),
        source_config_sha256=canonical_json_sha256(
            project.source_config.model_dump(mode="json")
        ),
        context_view_sha256=context_view.context_view_sha256,
        context_view_path=context_view.context_path,
        mandatory_glossary_sha256=live_mandatory_glossary_sha256(project),
        records=records,
    )
    write_judge_task(project, task)
    write_text_atomic(
        judge_task_source_block_path(project, task.judge_task_id),
        render_judge_task_block(task),
    )
    write_text_atomic(
        judge_ingest_block_path(project, task.judge_task_id),
        render_judge_task_block(task),
    )
    write_text_atomic(
        judge_ingest_json_path(project, task.judge_task_id),
        render_judge_ingest_json(task),
    )
    return task
