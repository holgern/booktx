"""Typer commands for judge/selection-profile workflows."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from booktx.cli_support import (
    _die,
    _handle_booktx_error,
    _load_runtime_or_exit,
    _project_relative,
    _project_status_snapshot,
    _reject_if_isolated,
    _require_chunks,
    _require_no_source_drift,
    _require_ready_context,
    console,
)
from booktx.errors import BooktxError
from booktx.workflows.judge import (
    accept_judge_submission_workflow,
    build_judge_status_workflow,
    create_judge_profile_workflow,
    create_next_judge_task_workflow,
    create_record_judge_task_workflow,
    judge_task_block_paths,
    judge_task_json_path,
)

judge_app = typer.Typer(help="Judge and selection-profile workflows.")


@judge_app.command(name="create-profile")
def judge_create_profile(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile_name: str = typer.Argument(..., help="Selection profile name."),
    target: str = typer.Option(..., "--target", help="Target language code."),
    target_locale: str | None = typer.Option(
        None, "--target-locale", help="Target locale."
    ),
    sources: str = typer.Option(
        ..., "--sources", help="Comma-separated source profiles."
    ),
    model: str | None = typer.Option(None, "--model", help="Judge model label."),
    select: bool = typer.Option(False, "--select", help="Select the created profile."),
) -> None:
    runtime = _load_runtime_or_exit(project_dir, require_profile=False)
    _reject_if_isolated(runtime)
    try:
        project = create_judge_profile_workflow(
            runtime.project.root,
            profile_name,
            target_language=target,
            target_locale=target_locale,
            sources_csv=sources,
            model=model,
            select=select,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(f"created selection profile: {project.profile}")
    if select:
        console.print(f"selected active profile: {project.profile}")


@judge_app.command(name="status")
def judge_status(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Selection profile name."
    ),
    sources: str | None = typer.Option(
        None, "--sources", help="Comma-separated source profiles."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    _reject_if_isolated(runtime)
    try:
        payload = build_judge_status_workflow(
            runtime.project,
            runtime,
            bundle=_project_status_snapshot(runtime.project),
            sources_csv=sources,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    console.print(f"selection profile: {payload['profile']}")
    console.print("source profiles: " + ", ".join(payload["source_profiles"]))
    console.print(
        f"records selected: {payload['records_selected']}/{payload['records_total']}"
    )
    console.print(f"records missing: {payload['records_missing']}")
    console.print(
        f"records with candidate gaps: {payload['records_with_candidate_gaps']}"
    )
    if payload["next_command"]:
        console.print(
            f"next command: {payload['next_command']}", soft_wrap=True, markup=False
        )


@judge_app.command(name="next")
def judge_next(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Selection profile name."
    ),
    sources: str | None = typer.Option(
        None, "--sources", help="Comma-separated source profiles."
    ),
    unit: str = typer.Option("chapter", "--unit", help="Task unit; currently chapter."),
    chapter: str | None = typer.Option(None, "--chapter", help="Optional chapter id."),
    max_words: int = typer.Option(900, "--max-words", help="Maximum source words."),
    output_format: str = typer.Option("block", "--format", help="block|json."),
    require_all_sources: bool = typer.Option(
        False,
        "--require-all-sources",
        help=(
            "Fail if any selected record is missing a candidate from a source profile."
        ),
    ),
) -> None:
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    _reject_if_isolated(runtime)
    proj = runtime.project
    _require_chunks(proj)
    _require_no_source_drift(proj)
    _require_ready_context(proj)
    if unit != "chapter":
        _die("--unit must be chapter")
    try:
        task = create_next_judge_task_workflow(
            proj,
            bundle=_project_status_snapshot(proj),
            sources_csv=sources,
            chapter=chapter,
            max_words=max_words,
            require_all_sources=require_all_sources,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    src_path, ingest_block = judge_task_block_paths(proj, task)
    edit_path = (
        ingest_block if output_format == "block" else judge_task_json_path(proj, task)
    )
    console.print(f"judge task: {task.judge_task_id}")
    console.print(f"records: {len(task.records)}")
    console.print(
        f"read:   {_project_relative(Path(src_path), proj.root)}",
        soft_wrap=True,
        markup=False,
    )
    console.print(
        f"edit:   {_project_relative(Path(edit_path), proj.root)}",
        soft_wrap=True,
        markup=False,
    )
    console.print(
        f"submit: booktx judge insert . --profile {proj.profile} "
        f"--judge-task-id {task.judge_task_id} "
        f"--file {_project_relative(Path(edit_path), proj.root)} "
        f"--format {output_format}",
        soft_wrap=True,
        markup=False,
    )


@judge_app.command(name="record")
def judge_record(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    record_id: str = typer.Option(..., "--record", help="Record id to judge."),
    profile: str | None = typer.Option(
        None, "--profile", help="Selection profile name."
    ),
    sources: str | None = typer.Option(
        None, "--sources", help="Comma-separated source profiles."
    ),
    require_all_sources: bool = typer.Option(
        False,
        "--require-all-sources",
        help="Fail if the record is missing a candidate from a source profile.",
    ),
) -> None:
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    _reject_if_isolated(runtime)
    proj = runtime.project
    _require_chunks(proj)
    _require_no_source_drift(proj)
    _require_ready_context(proj)
    try:
        task = create_record_judge_task_workflow(
            proj,
            bundle=_project_status_snapshot(proj),
            sources_csv=sources,
            record_id=record_id,
            require_all_sources=require_all_sources,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    src_path, ingest_block = judge_task_block_paths(proj, task)
    console.print(f"judge task: {task.judge_task_id}")
    console.print(
        f"read:   {_project_relative(Path(src_path), proj.root)}",
        soft_wrap=True,
        markup=False,
    )
    console.print(
        f"edit:   {_project_relative(Path(ingest_block), proj.root)}",
        soft_wrap=True,
        markup=False,
    )
    console.print(
        f"submit: booktx judge insert . --profile {proj.profile} "
        f"--judge-task-id {task.judge_task_id} "
        f"--file {_project_relative(Path(ingest_block), proj.root)} "
        "--format block",
        soft_wrap=True,
        markup=False,
    )


@judge_app.command(name="insert")
def judge_insert(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    judge_task_id: str = typer.Option(..., "--judge-task-id", help="Judge task id."),
    file: Path = typer.Option(..., "--file", help="Judge submission file."),
    profile: str | None = typer.Option(
        None, "--profile", help="Selection profile name."
    ),
    input_format: str = typer.Option("block", "--format", help="block|json."),
) -> None:
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    _reject_if_isolated(runtime)
    proj = runtime.project
    _require_chunks(proj)
    _require_no_source_drift(proj)
    _require_ready_context(proj)
    file_path = file if file.is_absolute() else (proj.root / file).resolve()
    try:
        payload = accept_judge_submission_workflow(
            proj,
            bundle=_project_status_snapshot(proj),
            judge_task_id=judge_task_id,
            file=file_path,
            input_format=input_format,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(f"accepted: {payload['accepted_records']} record(s)")
    if payload["version_refs"]:
        console.print("versions: " + ", ".join(payload["version_refs"]))
