"""Workflow layer for judge/selection-profile commands."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from booktx.acceptance import SubmissionValidationError
from booktx.config import (
    _err,
    load_judge_task,
    load_profile_config,
    write_profile_config,
)
from booktx.errors import BooktxError
from booktx.judge_acceptance import (
    accept_judge_submission,
    parse_judge_block_submission,
    parse_judge_json_submission,
)
from booktx.judge_store import (
    load_source_profile_projects,
    record_has_candidate_gap,
    resolve_selection_sources,
    selected_record_ids,
)
from booktx.judge_tasks import create_judge_task
from booktx.models import SelectionConfig
from booktx.workflows.profile import create_profile_workflow

if TYPE_CHECKING:
    from booktx.config import Project
    from booktx.runtime import RuntimeContext
    from booktx.status import StatusBundle

__all__ = [
    "build_judge_status_workflow",
    "create_judge_profile_workflow",
    "create_next_judge_task_workflow",
    "create_record_judge_task_workflow",
    "judge_task_block_paths",
    "judge_task_json_path",
    "accept_judge_submission_workflow",
]


def create_judge_profile_workflow(
    project_dir: Path,
    profile_name: str,
    *,
    target_language: str,
    target_locale: str | None,
    sources_csv: str | None,
    model: str | None,
    select: bool,
) -> Project:
    project = create_profile_workflow(
        project_dir,
        profile_name,
        target_language=target_language,
        target_locale=target_locale,
        model=model,
        select=select,
        kind="selection",
    )
    cfg = load_profile_config(project.root, profile_name)
    cfg.selection = SelectionConfig(sources=resolve_sources_csv(sources_csv))
    write_profile_config(project.root, cfg)
    return project


def resolve_sources_csv(sources_csv: str | None) -> list[str]:
    from booktx.judge_store import parse_sources_csv

    values = parse_sources_csv(sources_csv)
    if not values:
        raise _err("judge_sources_missing", "--sources must not be empty")
    return values


def build_judge_status_workflow(
    proj: Project,
    runtime: RuntimeContext,
    *,
    bundle: StatusBundle,
    sources_csv: str | None,
) -> dict[str, Any]:
    source_profiles = resolve_selection_sources(proj, sources_csv)
    source_projects = load_source_profile_projects(proj, source_profiles)
    selected_ids = selected_record_ids(proj)
    chapters: list[dict[str, Any]] = []
    candidate_gaps = 0
    next_chapter: str | None = None
    for chapter_id, record_ids in bundle.index.record_ids_by_chapter.items():
        total = len(record_ids)
        selected = sum(1 for record_id in record_ids if record_id in selected_ids)
        gaps = sum(
            1
            for record_id in record_ids
            if record_has_candidate_gap(source_projects, record_id)
        )
        candidate_gaps += gaps
        if next_chapter is None and selected < total:
            next_chapter = chapter_id
        chapter = bundle.index.chapters_by_id[chapter_id]
        chapters.append(
            {
                "chapter_id": chapter_id,
                "title": chapter.title,
                "selected_records": selected,
                "total_records": total,
                "missing_records": total - selected,
                "candidate_gap_records": gaps,
            }
        )
    sources_arg = ",".join(source_profiles)
    next_command = ""
    if next_chapter is not None:
        next_command = (
            f"booktx judge next . --profile {proj.profile} --sources {sources_arg} "
            f"--unit chapter --chapter {next_chapter} --max-words 900 --format block"
        )
    return {
        "profile": proj.profile or "",
        "source_profiles": source_profiles,
        "records_selected": len(selected_ids),
        "records_total": bundle.snapshot.totals.records_total,
        "records_missing": bundle.snapshot.totals.records_total - len(selected_ids),
        "records_with_candidate_gaps": candidate_gaps,
        "chapters": chapters,
        "next_command": next_command,
        "mode": runtime.mode.kind,
    }


def create_next_judge_task_workflow(
    proj: Project,
    *,
    bundle: StatusBundle,
    sources_csv: str | None,
    chapter: str | None,
    max_words: int,
    require_all_sources: bool,
) -> object:
    source_profiles = resolve_selection_sources(proj, sources_csv)
    effective_require_all_sources = _effective_require_all_sources(
        proj, require_all_sources
    )
    try:
        return create_judge_task(
            proj,
            bundle,
            source_profiles=source_profiles,
            chapter_id=chapter,
            record_id=None,
            max_words=max_words,
            require_all_sources=effective_require_all_sources,
        )
    except ValueError as exc:
        raise _err("judge_next", str(exc)) from exc


def _effective_require_all_sources(proj: Project, cli_value: bool) -> bool:
    cfg = proj.profile_config
    selection = cfg.selection if cfg is not None else None
    return cli_value or (
        selection.require_all_sources if selection is not None else False
    )


def create_record_judge_task_workflow(
    proj: Project,
    *,
    bundle: StatusBundle,
    sources_csv: str | None,
    record_id: str,
    require_all_sources: bool,
) -> object:
    source_profiles = resolve_selection_sources(proj, sources_csv)
    effective_require_all_sources = _effective_require_all_sources(
        proj, require_all_sources
    )
    try:
        return create_judge_task(
            proj,
            bundle,
            source_profiles=source_profiles,
            chapter_id=None,
            record_id=record_id,
            max_words=10**9,
            require_all_sources=effective_require_all_sources,
        )
    except ValueError as exc:
        raise _err("judge_record", str(exc)) from exc


def judge_task_block_paths(proj: Project, task: object) -> tuple[str, str]:
    from booktx.config import judge_ingest_block_path, judge_task_source_block_path

    judge_task_id = task.judge_task_id
    return (
        str(judge_task_source_block_path(proj, judge_task_id)),
        str(judge_ingest_block_path(proj, judge_task_id)),
    )


def judge_task_json_path(proj: Project, task: object) -> str:
    from booktx.config import judge_ingest_json_path

    return str(judge_ingest_json_path(proj, task.judge_task_id))


def accept_judge_submission_workflow(
    proj: Project,
    *,
    bundle: StatusBundle,
    judge_task_id: str,
    file: Path,
    input_format: str,
) -> dict[str, Any]:
    task = load_judge_task(proj, judge_task_id)
    if task is None:
        raise _err("judge_task_not_found", f"judge task not found: {judge_task_id}")
    text = file.read_text("utf-8")
    if input_format == "json":
        payload_task_id, submitted = parse_judge_json_submission(text)
    elif input_format == "block":
        payload_task_id, submitted = parse_judge_block_submission(text)
    else:
        raise _err("judge_format", "--format must be block or json")
    if payload_task_id and payload_task_id != judge_task_id:
        raise _err(
            "judge_task_id_mismatch",
            f"submission judge_task_id {payload_task_id} does not match "
            f"{judge_task_id}",
        )
    try:
        result = accept_judge_submission(
            proj,
            task,
            submitted,
            bundle=bundle,
        )
    except SubmissionValidationError as exc:
        raise BooktxError(
            "judge_submission_validation",
            "judge submission failed validation: "
            + "; ".join(f.message for f in exc.findings),
        ) from exc
    return {
        "accepted_records": result.accepted_records,
        "version_refs": result.version_refs,
    }
