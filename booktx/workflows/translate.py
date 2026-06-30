# ruff: noqa: B006,C901,E501
"""Domain workflow functions for the translation workflow (Phase 3 slice 7).

Wraps the translation_store / config / agent_todo / todo_resume / submissions
service layers so the Typer command layer never imports ``booktx.config``,
``booktx.translation_store``, or the forbidden write helpers directly.
The workflows own all store mutations; the thin Typer commands in
:mod:`booktx.commands.translate` parse options, invoke one workflow, and map
:class:`booktx.errors.BooktxError` to exit codes.

Workflows use the shared :data:`booktx.cli_support.console` for rendering
(the original commands interleaved rendering with mutations; splitting every
print call into a result object would add abstraction without behavior change).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from pydantic import ValidationError

from booktx.acceptance import (
    SubmissionValidationError,
    SubmittedRecord,
    accept_one_record,
    accept_translation_records,
)
from booktx.agent_todo import build_translation_todo, write_translation_todo
from booktx.cli_support import (
    _block_on_epub_audit_errors,
    _create_translation_task,
    _die,
    _editor_index_summary,
    _handle_booktx_error,
    _ledger_metadata_for_version,
    _load_project_or_exit,
    _load_runtime_or_exit,
    _load_translation_task_or_exit,
    _maybe_auto_export_indexes,
    _ordered_source_records,
    _print_todo_status_human,
    _print_translate_task,
    _project_relative,
    _project_status_snapshot,
    _render_submission_failures,
    _require_chunks,
    _require_no_source_drift,
    _require_ready_context,
    _resolved_identity,
    _select_translation_record_ids,
    _selected_chapter,
    _staged_preflight_check,
    _store_record_payload,
    _submission_ingest_hint,
    console,
)
from booktx.command_hints import (
    build_command,
    check_command,
    context_chapter_note_command,
    translate_next_command,
    translate_todo_resume_command,
    translate_todo_status_command,
)
from booktx.config import (
    identity_path,
    load_translation_store,
    project_source_sha256,
    translation_ingest_block_path,
    translation_ingest_path,
    translation_store_path,
    translation_task_source_block_path,
    write_identity,
    write_translation_store,
)
from booktx.context import ensure_context_view_snapshot, load_context
from booktx.editor_indexes import EditorIndexError, export_editor_indexes
from booktx.errors import BooktxError
from booktx.models import (
    StoredTranslationRecordV2,
    TranslatedChunk,
    TranslatedRecord,
    TranslationCandidate,
    TranslationIdentity,
    TranslationReviewCandidate,
    TranslationStore,
)
from booktx.path_display import display_path
from booktx.progress import (
    SourceRecordView,
    load_source_chunks,
    load_source_records,
    source_record_sha256,
)
from booktx.record_refs import parse_record_ref, resolve_record_range
from booktx.status import build_status_snapshot
from booktx.submissions import resolve_submission
from booktx.todo_resume import (
    resolve_translation_todo,
    resume_translation_todo,
)
from booktx.todo_status import (
    build_todo_status,
    current_todo_chapter_id,
    load_translation_todo,
)
from booktx.translation_store import (
    active_candidate,
    active_review_candidate,
    ensure_store_record,
    find_candidate,
    find_review_candidate,
    migrate_legacy_store,
    upsert_translation_version,
)
from booktx.validate import (
    Finding,
    Severity,
    load_validation_context,
    strict_load_translated,
    validate_chunk_pair,
    validate_project,
    validate_record_pair,
)
from booktx.versioning import resolve_current_version

if TYPE_CHECKING:
    pass


def translate_next_workflow(
    project_dir: Path,
    profile: str | None = None,
    chapter: str | None = None,
    unit: str = "paragraph",
    max_words: int = 900,
    as_json: bool = False,
    output_format: str = "text",
    show_sources: bool = False,
    show_template: bool = False,
    allow_missing_context: bool = False,
    chapter_word_limit: int | None = None,
    large_chapter_mode: str = "todo",
    force_chapter: bool = False,
) -> None:
    """Return the next text to translate and persist a task id."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    if unit not in {"paragraph", "batch", "chunk", "chapter"}:
        _die("--unit must be paragraph, batch, chunk, or chapter")
    if output_format not in {"text", "tsv", "block"}:
        _die("--format must be text, tsv, or block")
    if as_json and output_format != "text":
        _die("--json cannot be combined with --format")
    _require_chunks(proj)
    _require_no_source_drift(proj)
    _require_ready_context(proj, allow_missing_context=allow_missing_context)
    summary = _project_status_snapshot(proj)
    _block_on_epub_audit_errors(summary)
    selected_chapter = _selected_chapter(summary, chapter)
    if selected_chapter is None:
        console.print("All records already have accepted translations.")
        raise typer.Exit(code=1)
    # Large-chapter protection: when --unit chapter is requested and the
    # chapter exceeds the safe word budget, redirect to a single-chapter todo.
    if unit == "chapter" and not force_chapter:
        limit = chapter_word_limit or max_words
        if selected_chapter.source_words_remaining > limit:
            from booktx.todo_resume import ensure_single_chapter_todo

            if large_chapter_mode == "error":
                from booktx.command_hints import (
                    profile_option_fragment,
                    translate_todo_resume_command,
                )

                console.print(
                    f"Chapter {selected_chapter.chapter_id} has "
                    f"{selected_chapter.source_words_remaining:,} source words remaining, "
                    f"exceeding the safe budget of {limit}."
                )
                prof = profile_option_fragment(proj, runtime.mode)
                console.print("Create a bounded todo:")
                console.print(
                    f"booktx translate todo-next .{prof}"
                    f" --start-chapter {selected_chapter.chapter_id}"
                    f" --chapters 1 --batch-words {max_words} --write",
                    soft_wrap=True,
                    markup=False,
                )
                console.print("Resume the todo:")
                console.print(
                    translate_todo_resume_command(
                        proj,
                        mode=runtime.mode,
                        latest=True,
                    ),
                    soft_wrap=True,
                    markup=False,
                )
                raise typer.Exit(code=1)
            # large_chapter_mode == "todo" (default)
            todo = ensure_single_chapter_todo(
                proj,
                summary,
                chapter_id=selected_chapter.chapter_id,
                batch_words=max_words,
            )
            console.print(
                f"large chapter detected: {selected_chapter.chapter_id} "
                f"{selected_chapter.title} has "
                f"{selected_chapter.source_words_remaining:,} source words remaining"
            )
            console.print(f"created todo: {todo.todo_id}")
            console.print(
                f"goal: complete chapter {selected_chapter.chapter_id} {selected_chapter.title}"
            )
            console.print(f"batch words: {todo.batch_words}")
            from booktx.todo_resume import resume_translation_todo

            task = resume_translation_todo(
                proj, summary, mode=runtime.mode, todo_id=todo.todo_id
            )
            _print_translate_task(
                task,
                proj,
                mode=runtime.mode,
                as_json=as_json,
                output_format=output_format,
                show_sources=show_sources,
                show_template=show_template,
            )
            return
    actual_unit, record_ids = _select_translation_record_ids(
        summary,
        selected_chapter,
        unit=unit,
        max_words=max_words,
    )
    if not record_ids:
        console.print("Selected chapter has no remaining records.")
        raise typer.Exit(code=1)
    task = _create_translation_task(
        proj,
        summary,
        selected_chapter,
        mode=runtime.mode,
        unit=actual_unit,
        record_ids=record_ids,
        requested_max_words=max_words,
    )
    _print_translate_task(
        task,
        proj,
        mode=runtime.mode,
        as_json=as_json,
        output_format=output_format,
        show_sources=show_sources,
        show_template=show_template,
    )


def translate_insert_workflow(
    project_dir: Path,
    profile: str | None = None,
    task_id: str | None = None,
    stdin: bool = False,
    record_id: str | None = None,
    target: str | None = None,
    json_file: Path | None = None,
    input_file: Path | None = None,
    input_format: str = "json",
    allow_missing_context: bool = False,
    export_index: bool = False,
) -> None:
    """Accept translated text through the CLI and write the store atomically."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    if input_format not in {"json", "tsv", "block"}:
        _die("--format must be json, tsv, or block")
    _require_chunks(proj)
    _require_no_source_drift(proj)
    _require_ready_context(proj, allow_missing_context=allow_missing_context)

    try:
        parsed = resolve_submission(
            record_id=record_id,
            target=target,
            input_format=input_format,
            stdin=stdin,
            json_file=json_file,
            input_file=input_file,
            ingest_hint=_submission_ingest_hint(proj, task_id, mode=runtime.mode),
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return

    submitted_records = parsed.records
    payload_task_id = parsed.task_id

    effective_task_id = task_id or payload_task_id
    task = (
        _load_translation_task_or_exit(proj, effective_task_id)
        if effective_task_id
        else None
    )
    summary = _project_status_snapshot(proj)
    # Pre-write EPUB inline-XHTML check (Q2=a).
    # Stage submitted records and run the preflight BEFORE writing the store.
    submitted_ids = {r.id for r in submitted_records}
    try:
        _staged_preflight_check(proj, submitted_records, submitted_ids)
    except ValidationError as exc:
        console.print(
            "[red]error:[/red] internal preflight staging failed while "
            "validating submitted EPUB inline XHTML"
        )
        console.print(
            "hint: retry after updating booktx; the staged EPUB model could "
            "not be built. Run with debug output if available for traceback details."
        )
        console.print(f"detail: {exc}")
        raise typer.Exit(code=1) from None
    try:
        result = accept_translation_records(
            proj,
            submitted_records,
            bundle=summary,
            task=task,
            submission_translation_version=parsed.translation_version,
            submission_profile=parsed.profile,
            enforce_task_version=True,
        )
    except SubmissionValidationError as exc:
        _render_submission_failures(exc.findings)
        raise typer.Exit(code=1) from None
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(
        f"accepted: {result.accepted_records} record(s), "
        f"{result.target_words} target word(s)"
    )
    if result.version_ref:
        console.print(f"version: {result.version_ref}")
    _maybe_auto_export_indexes(proj, export_index=export_index, trigger="translation")
    if result.chapter_id:
        console.print(f"chapter: {result.chapter_id} {result.chapter_title}".rstrip())
        console.print(
            f"progress: {result.records_translated} / "
            f"{result.records_total} records translated, "
            f"{result.records_remaining} remaining"
        )
        if result.records_remaining == 0:
            console.print(
                f"chapter complete: {result.chapter_id} {result.chapter_title}".rstrip()
            )
            console.print("recommended context update template:")
            console.print(
                context_chapter_note_command(
                    proj,
                    mode=runtime.mode,
                    chapter_id=result.chapter_id,
                    title=result.chapter_title or "<TITLE>",
                ),
                soft_wrap=True,
                markup=False,
            )

    # Rebuild status after insert to get fresh totals.
    fresh = _project_status_snapshot(proj)
    max_words = task.requested_max_words if task and task.requested_max_words else 800
    if task is not None and task.todo_id:
        todo = load_translation_todo(proj, task.todo_id)
        if todo is None:
            console.print(
                f"[yellow]warning:[/yellow] todo {task.todo_id} referenced by task "
                f"{task.task_id} is missing; falling back to generic next hints"
            )
        else:
            todo_status = build_todo_status(
                proj,
                todo,
                fresh,
                fail_on_warnings=False,
            )
            if todo_status.goal_complete:
                console.print(f"todo complete: {todo.todo_id}")
                console.print("next: stop - todo goal complete")
            elif todo_status.next_safe_command is not None:
                console.print(
                    "next: " + todo_status.next_safe_command,
                    soft_wrap=True,
                    markup=False,
                )
            return
    if fresh.snapshot.totals.records_remaining == 0:
        console.print(
            "next: " + build_command(proj, mode=runtime.mode),
            soft_wrap=True,
            markup=False,
        )
    elif result.chapter_id and result.records_remaining > 0:
        # Current chapter still incomplete — stay on it.
        # Warn if this looks like an oversized chapter task (no todo backing).
        if task is not None and not task.todo_id and task.unit == "chapter":
            from booktx.command_hints import profile_option_fragment

            console.print(
                "[yellow]warning:[/yellow] this looks like an oversized chapter task. "
                "Use a bounded todo instead:"
            )
            prof = profile_option_fragment(proj, runtime.mode)
            console.print(
                f"booktx translate todo-next .{prof}"
                f" --start-chapter {result.chapter_id}"
                f" --chapters 1 --batch-words {max_words} --write",
                soft_wrap=True,
                markup=False,
            )
            console.print(
                f"booktx translate todo-resume .{prof} --latest --format block",
                soft_wrap=True,
                markup=False,
            )
        console.print(
            "next: "
            + translate_next_command(
                proj,
                mode=runtime.mode,
                chapter_id=result.chapter_id,
                max_words=max_words,
            ),
            soft_wrap=True,
            markup=False,
        )
    elif result.chapter_id:
        # Chapter just completed — advance to next incomplete chapter.
        console.print(
            "next: "
            + translate_next_command(
                proj,
                mode=runtime.mode,
                max_words=max_words,
            ),
            soft_wrap=True,
            markup=False,
        )


def translate_todo_next_workflow(
    project_dir: Path,
    profile: str | None = None,
    chapters: int = 3,
    batch_words: int = 800,
    max_run_words: int | None = None,
    start_chapter: str | None = None,
    skip_current: bool = False,
    write: bool = False,
    as_json: bool = False,
) -> None:
    """Create a durable run-control todo for a bounded multi-chapter translation run.

    This writes a todo file (not translations) describing how many chapters to
    complete, the per-task word budget, and the stop conditions.  The agent
    reads the todo and loops ``translate next -> fill -> insert -> validate``
    until done or a stop condition occurs.
    """

    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    _require_chunks(proj)
    _require_no_source_drift(proj)
    _require_ready_context(proj)
    bundle = _project_status_snapshot(proj)
    _block_on_epub_audit_errors(bundle)

    try:
        todo = build_translation_todo(
            proj,
            bundle,
            chapters=chapters,
            batch_words=batch_words,
            max_run_words=max_run_words,
            skip_current=skip_current,
            start_chapter=start_chapter,
        )
    except ValueError as exc:
        _die(str(exc))

    json_path: Path | None = None
    md_path: Path | None = None
    if write:
        json_path, md_path = write_translation_todo(proj, todo, mode=runtime.mode)
        # Verify the written file is loadable before printing success.
        loaded = load_translation_todo(proj, todo.todo_id)
        if loaded is None:
            _die(f"internal error: wrote todo {todo.todo_id} but could not reload it")

    if as_json:
        payload: dict[str, object] = {
            "version": 1,
            "todo_id": todo.todo_id,
            "profile": todo.profile,
            "target_language": todo.target_language,
            "target_locale": todo.target_locale,
            "chapters_requested": todo.chapters_requested,
            "batch_words": todo.batch_words,
            "max_run_words": todo.max_run_words,
            "include_current": todo.include_current,
            "created_at": todo.created_at,
            "baseline_ref": todo.baseline_ref,
            "baseline_sha256": todo.baseline_sha256,
            "context_sha256": todo.context_sha256,
            "source_sha256": todo.source_sha256,
            "chapters": [
                {
                    "chapter_id": c.chapter_id,
                    "title": c.title,
                    "status": c.status,
                    "records_total": c.records_total,
                    "records_translated_at_start": c.records_translated_at_start,
                    "records_remaining_at_start": c.records_remaining_at_start,
                    "source_words_remaining_at_start": c.source_words_remaining_at_start,
                    "pending_chunk_ids": c.pending_chunk_ids,
                }
                for c in todo.chapters
            ],
        }
        if json_path is not None:
            payload["json_path"] = display_path(json_path, runtime.mode)
        if md_path is not None:
            payload["markdown_path"] = display_path(md_path, runtime.mode)
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    # Human output
    console.print(f"todo: {todo.todo_id}")
    first = todo.chapters[0] if todo.chapters else None
    if first:
        console.print(
            f"goal: complete {todo.chapters_requested} incomplete chapter(s),"
            f" starting at {first.chapter_id} {first.title}".rstrip()
        )
    else:
        console.print(f"goal: complete {todo.chapters_requested} incomplete chapter(s)")
    console.print(f"batch words: {todo.batch_words}")
    console.print("chapters: " + ", ".join(c.chapter_id for c in todo.chapters))
    if md_path is not None:
        console.print(
            f"markdown: {display_path(md_path, runtime.mode)}",
            soft_wrap=True,
            markup=False,
        )
    if json_path is not None:
        console.print(
            f"json: {display_path(json_path, runtime.mode)}",
            soft_wrap=True,
            markup=False,
        )
    console.print(
        "next command: "
        + translate_todo_status_command(proj, mode=runtime.mode, todo_id=todo.todo_id),
        soft_wrap=True,
        markup=False,
    )
    console.print(
        "resume command: "
        + translate_todo_resume_command(
            proj,
            mode=runtime.mode,
            todo_id=todo.todo_id,
            output_format="block",
        ),
        soft_wrap=True,
        markup=False,
    )


def translate_todo_status_workflow(
    project_dir: Path,
    profile: str | None = None,
    todo_id: str | None = None,
    latest: bool = False,
    as_json: bool = False,
) -> None:
    """Show live bounded-run todo status and the next safe command."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    _require_chunks(proj)
    bundle = _project_status_snapshot(proj)
    try:
        todo = resolve_translation_todo(proj, bundle, todo_id=todo_id, latest=latest)
        scope_chapter = current_todo_chapter_id(todo, bundle)
        scoped_report = validate_project(proj, chapter_id=scope_chapter)
        # Second full pass for the non-blocking global note (ac-0003).
        global_report = validate_project(proj) if scope_chapter is not None else None
        status = build_todo_status(
            proj,
            todo,
            bundle,
            mode=runtime.mode,
            validation_report=scoped_report,
            fail_on_warnings=True,
            scope_chapter_id=scope_chapter,
            global_report=global_report,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    if as_json:
        console.print_json(json.dumps(status.as_dict(), ensure_ascii=False))
        return
    _print_todo_status_human(status)


def translate_todo_resume_workflow(
    project_dir: Path,
    profile: str | None = None,
    todo_id: str | None = None,
    latest: bool = False,
    as_json: bool = False,
    output_format: str = "block",
    show_sources: bool = False,
    show_template: bool = False,
) -> None:
    """Resume a bounded multi-chapter todo and create the next safe task."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    if output_format not in {"text", "tsv", "block"}:
        _die("--format must be text, tsv, or block")
    if as_json and output_format != "text":
        _die("--json cannot be combined with --format")
    _require_chunks(proj)
    bundle = _project_status_snapshot(proj)
    try:
        task = resume_translation_todo(
            proj, bundle, mode=runtime.mode, todo_id=todo_id, latest=latest
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    _print_translate_task(
        task,
        proj,
        mode=runtime.mode,
        as_json=as_json,
        output_format=output_format,
        show_sources=show_sources,
        show_template=show_template,
    )


def translate_import_legacy_workflow(
    project_dir: Path,
    profile: str | None = None,
) -> None:
    """Import valid legacy translated chunk files into the translation store."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    _require_chunks(proj)
    store = load_translation_store(proj)
    resolution = resolve_current_version(
        proj,
        note="Imported valid legacy translated chunks into nested translation store.",
    )
    imported_records = 0
    imported_chunks = 0
    source_chunks = {chunk.chunk_id: chunk for chunk in load_source_chunks(proj)}
    for chunk_id, source_chunk in source_chunks.items():
        if proj.translated_dir is None:
            continue
        path = proj.translated_dir / f"{chunk_id}.json"
        if not path.is_file():
            continue
        findings = validate_chunk_pair(source_chunk, path, load_context(proj))
        if any(finding.severity == Severity.ERROR for finding in findings):
            continue
        translated_chunk, err = strict_load_translated(path)
        if err is not None or translated_chunk is None:
            continue
        imported_chunks += 1
        source_records = {record.id: record for record in source_chunk.records}
        for record in translated_chunk.records:
            source_record = source_records[record.id]
            stored = ensure_store_record(
                store,
                record.id,
                source=source_record.source,
                source_sha256=source_record_sha256(source_record.source),
            )
            if active_candidate(stored) is not None:
                continue
            upsert_translation_version(
                stored,
                resolution.version_ref,
                record.target,
                updated_at=datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            )
            imported_records += 1
    store.source_sha256 = project_source_sha256(proj)
    write_translation_store(proj, store)
    console.print(
        f"imported: {imported_records} record(s) from {imported_chunks} legacy chunk(s)"
    )


def translate_migrate_store_workflow(
    project_dir: Path,
    profile: str | None = None,
    write: bool = False,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    context_label: str | None = None,
    allow_missing_source: bool = False,
) -> None:
    """Inspect or rewrite a legacy translation-store.json into the v2 schema."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    path = translation_store_path(proj)
    if not path.is_file():
        _die("translation-store.json is missing")

    try:
        raw = json.loads(path.read_text("utf-8"))
        if isinstance(raw, dict) and raw.get("version") == 2:
            console.print("translation-store.json is already v2")
            return
        legacy = TranslationStore.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        _die(f"translation-store.json is invalid: {exc}")
        return

    source_records = {record.record_id: record for record in load_source_records(proj)}
    migration = migrate_legacy_store(legacy, source_records=source_records)

    if not write:
        console.print(f"dry-run: would migrate {migration.migrated_records} record(s)")
        if migration.missing_source_ids:
            console.print(
                "missing source records: " + ", ".join(migration.missing_source_ids)
            )
        return

    if actor is not None or harness is not None or model is not None:
        write_identity(
            proj,
            TranslationIdentity(
                actor=actor or "user:unknown",
                harness=harness or "booktx",
                model=model or "human",
            ),
        )

    resolution = resolve_current_version(
        proj,
        actor=actor,
        harness=harness,
        model=model,
        context_label=context_label,
        note="Migrated legacy v1 translation store to v2 nested store.",
    )
    migration = migrate_legacy_store(
        legacy,
        source_records=source_records,
        version_ref=resolution.version_ref,
    )
    if migration.missing_source_ids and not allow_missing_source:
        _die(
            "cannot migrate store with missing source records: "
            + ", ".join(migration.missing_source_ids)
        )
    write_translation_store(proj, migration.store)
    console.print(
        f"migrated: {migration.migrated_records} record(s) to store v2 at "
        f"{_project_relative(path, proj.root)}"
    )
    console.print(f"version: {resolution.version_ref}")
    console.print(
        "ledger: "
        + _project_relative(
            proj.booktx_dir / "translation-version-ledger.json", proj.root
        )
    )
    if actor is not None or harness is not None or model is not None:
        console.print(f"identity: {_project_relative(identity_path(proj), proj.root)}")


def translate_export_workflow(
    project_dir: Path,
    profile: str | None = None,
    version_ref: str | None = None,
    track: int | None = None,
    latest_subversion: bool = False,
    all_versions: bool = False,
) -> None:
    """Export fully accepted store-backed chunks into translated/*.json."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    _require_chunks(proj)
    store = load_translation_store(proj)
    if all_versions and (version_ref is not None or track is not None):
        _die("--all-versions cannot be combined with --version or --track")
        return
    if track is not None and not latest_subversion:
        _die("--track currently requires --latest-subversion")
        return

    from booktx.io_utils import write_json_model_atomic

    def _pick_candidate(
        stored: StoredTranslationRecordV2,
    ) -> TranslationCandidate | None:
        if all_versions:
            return None
        if version_ref is not None:
            candidate = find_candidate(stored, version_ref)
            return (
                candidate
                if candidate is not None and candidate.status == "accepted"
                else None
            )
        if track is not None:
            matches = [
                candidate
                for candidate in stored.versions
                if candidate.version == track and candidate.status == "accepted"
            ]
            if not matches:
                return None
            return max(matches, key=lambda item: item.subversion)
        candidate = active_candidate(stored)
        return (
            candidate
            if candidate is not None and candidate.status == "accepted"
            else None
        )

    exported = 0
    if all_versions:
        version_map: dict[str, dict[str, list[TranslatedRecord]]] = {}
        for chunk in load_source_chunks(proj):
            for record in chunk.records:
                stored = store.records.get(record.id)
                if stored is None:
                    continue
                for candidate in stored.versions:
                    if candidate.status != "accepted":
                        continue
                    version_map.setdefault(candidate.version_ref, {}).setdefault(
                        chunk.chunk_id, []
                    ).append(
                        TranslatedRecord(
                            id=record.id,
                            version=candidate.version_ref,
                            target=candidate.target,
                        )
                    )
        for ref, chunks in version_map.items():
            if proj.translated_dir is None:
                continue
            export_dir = proj.translated_dir / ref
            export_dir.mkdir(parents=True, exist_ok=True)
            for chunk_id, records in chunks.items():
                write_json_model_atomic(
                    export_dir / f"{chunk_id}.json",
                    TranslatedChunk(chunk_id=chunk_id, records=records),
                )
                exported += 1
        console.print(f"exported: {exported} chunk file(s) to {proj.translated_dir}")
        return

    for chunk in load_source_chunks(proj):
        translated_records: list[TranslatedRecord] = []
        for record in chunk.records:
            stored = store.records.get(record.id)
            if stored is None:
                translated_records = []
                break
            picked = _pick_candidate(stored)
            if picked is None:
                translated_records = []
                break
            translated_records.append(
                TranslatedRecord(
                    id=record.id,
                    version=picked.version_ref,
                    target=picked.target,
                )
            )
        if not translated_records:
            continue
        translated_chunk = TranslatedChunk(
            chunk_id=chunk.chunk_id, records=translated_records
        )
        findings = []
        for source_record, translated_record in zip(
            chunk.records, translated_chunk.records, strict=True
        ):
            findings.extend(
                validate_record_pair(
                    source_record, translated_record, chunk.chunk_id, load_context(proj)
                )
            )
        if any(finding.severity == Severity.ERROR for finding in findings):
            continue
        if proj.translated_dir is None:
            continue
        write_json_model_atomic(
            proj.translated_dir / f"{chunk.chunk_id}.json", translated_chunk
        )
        exported += 1
    console.print(f"exported: {exported} chunk(s) to {proj.translated_dir}")


def translate_export_index_workflow(
    project_dir: Path,
    profile: str | None = None,
    kind: list[str] = [],
    fail_on_warn: bool = False,
    as_json: bool = False,
    jsonl: bool = False,
) -> None:
    """Export profile-local editor QA indexes.

    Writes generated, rebuildable artifacts under translations/<profile>/:
    source-index.json (source text only), target-index.json (target text only),
    and source-target-index.json (slim side-by-side view). All three are safe
    to delete and regenerate; the canonical state remains translation-store.json.
    """
    valid_kinds = {"source", "target", "source-target"}
    invalid = sorted({k for k in kind if k not in valid_kinds})
    if invalid:
        _die(
            f"invalid --kind value(s) {invalid}; expected one of {sorted(valid_kinds)}"
        )
        return
    requested = set(kind) if kind else None

    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    mode = runtime.mode

    def _display(path_str: str | None) -> str | None:
        if path_str is None:
            return None
        return display_path(Path(path_str), mode)

    try:
        result = export_editor_indexes(
            proj,
            kinds=requested,  # type: ignore[arg-type]  # validated against valid_kinds above; mypy cannot narrow set[str] -> set[Literal[...]]
            fail_on_warn=fail_on_warn,
            write_jsonl=jsonl,
        )
    except EditorIndexError as exc:
        # source-index may have been written before target-based export failed.
        partial = exc.result
        if partial.source_path is not None:
            console.print(
                f"exported source index: {partial.source_record_count} "
                f"record(s) to {_display(partial.source_path)}"
            )
        if as_json:
            payload = _editor_index_summary(partial, _display)
            payload["error"] = str(exc)
            console.print_json(json.dumps(payload, ensure_ascii=False))
        else:
            console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if as_json:
        console.print_json(
            json.dumps(_editor_index_summary(result, _display), ensure_ascii=False)
        )
        return

    if result.source_path is not None:
        console.print(
            f"exported source index: {result.source_record_count} record(s) "
            f"to {_display(result.source_path)}"
        )
    if result.target_path is not None:
        console.print(
            f"exported target index: {result.target_record_count} record(s) "
            f"to {_display(result.target_path)}"
        )
    if result.source_target_path is not None:
        console.print(
            f"exported source-target index: {result.source_target_record_count} "
            f"record(s) to {_display(result.source_target_path)}"
        )
    console.print(f"translated: {result.translated_count}")
    console.print(f"missing: {result.missing_count}")
    console.print(f"warnings: {result.warning_count}")
    console.print(f"errors: {result.error_count}")
    if jsonl:
        console.print("jsonl: written for requested successful indexes")


def translate_task_status_workflow(
    project_dir: Path,
    task_id: str,
    profile: str | None = None,
    as_json: bool = False,
) -> None:
    """Report accepted vs missing progress for one persisted translation task.

    Makes interrupted translation runs diagnosable without inspecting the store
    by hand. Exits 0 only when every task record is accepted and current.
    """
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    _require_chunks(proj)
    task = _load_translation_task_or_exit(proj, task_id)
    store = load_translation_store(proj)

    accepted_ids: list[str] = []
    missing_ids: list[str] = []
    stale_ids: list[str] = []
    for record in task.records:
        stored = store.records.get(record.id)
        if stored is None:
            missing_ids.append(record.id)
            continue
        expected_sha = source_record_sha256(record.source)
        if stored.source_sha256 and stored.source_sha256 != expected_sha:
            stale_ids.append(record.id)
            continue
        candidate = active_candidate(stored)
        if candidate is None or candidate.status != "accepted":
            missing_ids.append(record.id)
            continue
        accepted_ids.append(record.id)

    total = len(task.records)
    accepted = len(accepted_ids)
    not_current = total - accepted
    first_missing = (
        missing_ids[0] if missing_ids else (stale_ids[0] if stale_ids else None)
    )
    complete = not_current == 0

    source_display = _project_relative(
        translation_task_source_block_path(proj, task.task_id), proj.root
    )
    block_ingest_display = _project_relative(
        translation_ingest_block_path(proj, task.task_id), proj.root
    )
    json_ingest_display = _project_relative(
        translation_ingest_path(proj, task.task_id), proj.root
    )
    from booktx.command_hints import translate_insert_command

    submit_hint = translate_insert_command(
        proj,
        task_id=task.task_id,
        file_path=block_ingest_display,
        input_format="block",
    )

    payload = {
        "version": 1,
        "task_id": task.task_id,
        "chapter_id": task.chapter_id,
        "chapter_title": task.chapter_title,
        "records_total": total,
        "records_accepted": accepted,
        "records_missing": len(missing_ids),
        "records_stale": len(stale_ids),
        "missing_ids": missing_ids,
        "stale_ids": stale_ids,
        "first_missing": first_missing,
        "complete": complete,
        "source_block_path": source_display,
        "block_ingest_path": block_ingest_display,
        "json_ingest_path": json_ingest_display,
        "submit_hint": submit_hint,
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        raise typer.Exit(code=0 if complete else 1)

    console.print(f"task: {task.task_id}")
    console.print(f"chapter: {task.chapter_id}  {task.chapter_title}".rstrip())
    console.print(f"records: {accepted} / {total} accepted, {not_current} missing")
    if stale_ids:
        console.print(f"stale: {len(stale_ids)} record(s) need re-translation")
    if first_missing is not None:
        console.print(f"first missing: {first_missing}")
    console.print(f"source file: {source_display}", soft_wrap=True, markup=False)
    console.print(f"ingest file: {block_ingest_display}", soft_wrap=True, markup=False)
    console.print(f"submit: {submit_hint}", soft_wrap=True, markup=False)
    raise typer.Exit(code=0 if complete else 1)


def translation_get_record_workflow(
    project_dir: Path,
    record_ref: str,
    before: int = 0,
    after: int = 0,
    version: str | None = None,
    profile: str | None = None,
    as_json: bool = False,
) -> None:
    """Inspect one source record with nearby context and available versions."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    selected, details = _store_record_payload(proj, record_ref)
    ordered = details["ordered"]
    ordered_ids = [record.record_id for record in ordered]
    selected_id = selected["id"]
    try:
        index = ordered_ids.index(selected_id)
    except ValueError:
        _die(f"unknown source record id: {selected_id}")
        return

    store = details["store"]

    def _record_payload(source_record: SourceRecordView) -> dict[str, Any]:
        payload = {
            "id": source_record.record_id,
            "chunk_id": source_record.chunk_id,
            "source": source_record.source,
        }
        stored = store.records.get(source_record.record_id)
        if stored is not None:
            payload["active_version"] = stored.active_version
            candidate = (
                find_candidate(stored, version)
                if version is not None
                else active_candidate(stored)
            )
            if candidate is not None:
                payload["target"] = candidate.target
                payload["status"] = candidate.status
                payload["version_ref"] = candidate.version_ref
        return payload

    before_records = [
        _record_payload(record) for record in ordered[max(0, index - before) : index]
    ]
    selected_payload = _record_payload(ordered[index])
    selected_payload["available_targets"] = details["versions"]
    selected_payload["ledger_metadata"] = _ledger_metadata_for_version(
        proj, version or selected_payload.get("active_version")
    )
    after_records = [
        _record_payload(record) for record in ordered[index + 1 : index + 1 + after]
    ]
    payload = {
        "selected_record_ref": selected_id,
        "before": before_records,
        "selected": selected_payload,
        "after": after_records,
        "available_targets": details["versions"],
        "active_version": selected_payload.get("active_version"),
        "ledger_metadata": selected_payload["ledger_metadata"],
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    for item in before_records:
        console.print(f"   {item['id']}  {item['source']}")
    console.print(f">> {selected_id}  {selected_payload['source']}")
    for candidate in details["versions"]:
        console.print(
            f"   {candidate['version_ref']} [{candidate['status']}] {candidate['target']}"
        )
    for item in after_records:
        console.print(f"   {item['id']}  {item['source']}")


def translation_list_workflow(
    project_dir: Path,
    range_spec: str | None = None,
    chapter: int | None = None,
    version: str | None = None,
    profile: str | None = None,
    as_json: bool = False,
) -> None:
    """List records for a range or chapter in source reading order."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    if (range_spec is None) == (chapter is None):
        _die("use exactly one of --range or --chapter")
        return
    ordered = _ordered_source_records(proj)
    ordered_ids = [record.record_id for record in ordered]
    ctx = load_context(proj)
    bundle = build_status_snapshot(
        proj,
        context_exists=ctx is not None,
        context_ready=bool(ctx and ctx.ready),
    )
    spec = range_spec if range_spec is not None else f"chapter:{chapter}"
    try:
        selected_ids = resolve_record_range(
            spec,
            ordered_record_ids=ordered_ids,
            chapter_record_ids=bundle.index.record_ids_by_chapter,
        )
    except ValueError as exc:
        _die(str(exc))
        return
    store = load_translation_store(proj)
    payload: list[dict[str, Any]] = []
    for record in ordered:
        if record.record_id not in selected_ids:
            continue
        item = {
            "id": record.record_id,
            "chunk_id": record.chunk_id,
            "source": record.source,
        }
        stored = store.records.get(record.record_id)
        if stored is not None:
            if stored.active_version is not None:
                item["active_version"] = stored.active_version
            candidate = (
                find_candidate(stored, version)
                if version is not None
                else active_candidate(stored)
            )
            if candidate is not None:
                item["target"] = candidate.target
                item["status"] = candidate.status
                item["version_ref"] = candidate.version_ref
        payload.append(item)
    if as_json:
        console.print_json(json.dumps({"records": payload}, ensure_ascii=False))
        return
    for item in payload:
        suffix = f" [{item['version_ref']}]" if "version_ref" in item else ""
        console.print(f"{item['id']}{suffix}  {item['source']}")


def translation_compare_workflow(
    project_dir: Path,
    record_ref: str,
    versions: str,
    profile: str | None = None,
    as_json: bool = False,
) -> None:
    """Compare multiple stored version candidates for one record."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    selected, details = _store_record_payload(proj, record_ref)
    store = details["store"]
    stored = store.records.get(selected["id"])
    if stored is None:
        _die(f"record {selected['id']} has no stored translations")
        return

    requested = [item.strip() for item in versions.split(",") if item.strip()]
    payload = {"record_ref": selected["id"], "comparisons": []}
    for ref in requested:
        if ref.startswith("R"):
            candidate: TranslationCandidate | TranslationReviewCandidate | None = (
                find_review_candidate(stored, ref)
            )
            kind = "review"
        else:
            candidate = find_candidate(stored, ref)
            kind = "translation"
        payload["comparisons"].append(
            {
                "ref": ref,
                "kind": kind,
                "target": candidate.target if candidate is not None else None,
                "status": candidate.status if candidate is not None else None,
            }
        )
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    for item in payload["comparisons"]:
        console.print(f"{item['ref']} {item['kind']}: {item['target'] or '<missing>'}")


def translation_activate_workflow(
    project_dir: Path,
    record_ref: str,
    version_ref: str,
    profile: str | None = None,
) -> None:
    """Activate one stored candidate version for a single record."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    store = load_translation_store(proj)
    record_id = parse_record_ref(record_ref).canonical_id
    stored = store.records.get(record_id)
    if stored is None:
        _die(f"record {record_id} has no stored translations")
        return
    candidate = find_candidate(stored, version_ref)
    if candidate is None:
        _die(f"record {record_id} has no version {version_ref}")
        return
    stored.active_version = candidate.version_ref
    write_translation_store(proj, store)
    console.print(f"{record_id} -> {candidate.version_ref}")


def translation_review_workflow(
    project_dir: Path,
    record_ref: str,
    activate: str | None = None,
    note: str | None = None,
    profile: str | None = None,
) -> None:
    """Review one stored candidate and optionally activate it."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    store = load_translation_store(proj)
    record_id = parse_record_ref(record_ref).canonical_id
    stored = store.records.get(record_id)
    if stored is None:
        _die(f"record {record_id} has no stored translations")
        return
    candidate = (
        find_candidate(stored, activate)
        if activate is not None
        else active_candidate(stored)
    )
    if candidate is None:
        _die(f"record {record_id} has no matching review target")
        return
    if activate is not None:
        stored.active_version = candidate.version_ref
    candidate.reviewed_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    candidate.reviewed_by = _resolved_identity(proj).actor
    candidate.review_note = note
    write_translation_store(proj, store)
    console.print(f"{record_id} reviewed {candidate.version_ref}")


def translate_set_record_workflow(
    project_dir: Path,
    task_id: str,
    record_id: str,
    profile: str | None = None,
    stdin: bool = False,
    target: str | None = None,
    allow_missing_context: bool = False,
) -> None:
    """Commit a single translated record from stdin (or --target).

    Lets an agent safely commit one record at a time so work already written to
    translation-store.json survives interruption. Prefer this over embedding a
    whole chapter section in one shell command when truncation is a concern.
    """
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    _require_chunks(proj)
    _require_ready_context(proj, allow_missing_context=allow_missing_context)
    task = _load_translation_task_or_exit(proj, task_id)
    if record_id not in {record.id for record in task.records}:
        _die(f"record {record_id} is not part of task {task.task_id}")

    if target is not None:
        target_text = target
    elif stdin:
        target_text = sys.stdin.read()
        # Drop a single trailing newline (common shell/heredoc artifact) while
        # preserving all internal multiline text.
        if target_text.endswith("\r\n"):
            target_text = target_text[:-2]
        elif target_text.endswith("\n"):
            target_text = target_text[:-1]
    else:
        _die("provide the target text with --stdin or --target")

    summary = _project_status_snapshot(proj)
    try:
        result = accept_one_record(
            proj,
            record_id,
            target_text,
            bundle=summary,
            task=task,
            submission_profile=task.profile or None,
        )
    except SubmissionValidationError as exc:
        _render_submission_failures(exc.findings)
        raise typer.Exit(code=1) from None
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(f"accepted: 1 record, {result.target_words} target word(s)")
    if result.chapter_id:
        console.print(f"chapter: {result.chapter_id} {result.chapter_title}".rstrip())
        console.print(
            f"progress: {result.records_translated} / "
            f"{result.records_total} records translated, "
            f"{result.records_remaining} remaining"
        )


@dataclass(frozen=True)
class CurrentWriteContext:
    version_ref: str
    baseline_ref: str
    baseline_sha256: str
    context_view_sha256: str
    context_view_path: str
    context_notes_scope: str
    context_target_chapter_id: str
    context_notes_through_chapter_id: str | None


def _current_write_contexts_for_records(proj: Any, *, bundle: Any, record_ids: set[str]) -> dict[str, CurrentWriteContext]:
    missing = sorted(rid for rid in record_ids if rid not in bundle.index.source_by_id or rid not in bundle.index.record_to_chapter)
    if missing:
        _die("unknown or unmapped record(s): " + ", ".join(missing))
        raise typer.Exit(code=1)
    resolution = resolve_current_version(proj)
    by_chapter = {bundle.index.record_to_chapter[rid] for rid in record_ids}
    contexts: dict[str, CurrentWriteContext] = {}
    for chapter_id in sorted(by_chapter):
        snap = ensure_context_view_snapshot(
            proj,
            baseline_ref=resolution.version_ref,
            baseline_sha256=resolution.baseline_sha256,
            target_chapter_id=chapter_id,
        )
        contexts[chapter_id] = CurrentWriteContext(
            version_ref=resolution.version_ref,
            baseline_ref=resolution.version_ref,
            baseline_sha256=resolution.baseline_sha256,
            context_view_sha256=snap.context_view_sha256,
            context_view_path=snap.context_path,
            context_notes_scope=snap.notes_scope,
            context_target_chapter_id=snap.target_chapter_id,
            context_notes_through_chapter_id=snap.notes_through_chapter_id,
        )
    return contexts


def translation_revise_record_workflow(
    project_dir: Path,
    record_ref: str,
    profile: str | None = None,
    stdin: bool = False,
    target: str | None = None,
    activate: bool = True,
) -> None:
    """Revise an already accepted translation record safely.

    Validates the new target, runs staged EPUB inline-XHTML preflight
    (strict mode: warnings block), and writes through the store API.
    Never edits translation-store.json directly.
    """
    from booktx.io_utils import utc_timestamp

    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    _require_chunks(proj)
    _require_ready_context(proj)
    record_id = parse_record_ref(record_ref).canonical_id

    # Read target text.
    if target is not None:
        target_text = target
    elif stdin:
        target_text = sys.stdin.read()
        if target_text.endswith("\r\n"):
            target_text = target_text[:-2]
        elif target_text.endswith("\n"):
            target_text = target_text[:-1]
    else:
        _die("provide the target text with --stdin or --target")
        return  # unreachable, but keeps mypy happy

    if not target_text.strip():
        _die(f"empty target for record {record_id}")

    # Load store and check the record exists.
    store = load_translation_store(proj)
    stored = store.records.get(record_id)
    if stored is None:
        _die(f"record {record_id} has no stored translations")
    assert stored is not None

    # Reject when an active_review exists: the effective output would be
    # the review candidate, so revising the translation version is a
    # silent no-op. The user must clear or re-review explicitly.
    review = active_review_candidate(stored)
    if review is not None:
        _die(
            f"record {record_id} has active review {review.review_ref},"
            f" so changing active_version will not affect output."
            f" Use `booktx review revise-record . {record_ref} --base-review {review.review_ref}"
            f" --stdin` or `booktx review deactivate . {record_ref}`."
        )

    # Validate the record pair.
    bundle = _project_status_snapshot(proj)
    source_view = bundle.index.source_by_id.get(record_id)
    if source_view is None:
        _die(f"record {record_id} has no matching source record")
    assert source_view is not None
    source_chunks = bundle.index.source_chunks
    source_chunk = source_chunks.get(source_view.chunk_id)
    if source_chunk is None:
        _die(f"record {record_id} has no matching source chunk")
    assert source_chunk is not None
    source_record = next((r for r in source_chunk.records if r.id == record_id), None)
    if source_record is None:
        _die(f"record {record_id} not found in source chunk")
    assert source_record is not None
    translated = TranslatedRecord(id=record_id, target=target_text)
    context = load_validation_context(proj)
    pair_findings = validate_record_pair(
        source_record, translated, source_chunk.chunk_id, context
    )
    pair_errors = [f for f in pair_findings if f.severity == Severity.ERROR]
    if pair_errors:
        _render_submission_failures(pair_errors)
        raise typer.Exit(code=1)

    # Staged EPUB inline-XHTML preflight (strict: warnings also block).
    _staged_preflight_check(
        proj,
        [SubmittedRecord(id=record_id, target=target_text)],
        {record_id},
        fail_on_warnings=True,
    )

    # Resolve provenance and create chapter context snapshots before mutating the store.
    write_context = _current_write_contexts_for_records(proj, bundle=bundle, record_ids={record_id})[
        bundle.index.record_to_chapter[record_id]
    ]
    version_ref = write_context.version_ref
    ensure_store_record(
        store,
        record_id,
        source=source_view.source,
        source_sha256=source_view.source_sha256,
    )
    upsert_translation_version(
        store.records[record_id],
        version_ref,
        target_text,
        updated_at=utc_timestamp(),
        activate=activate,
        baseline_ref=write_context.baseline_ref,
        baseline_sha256=write_context.baseline_sha256,
        context_view_sha256=write_context.context_view_sha256,
        context_view_path=write_context.context_view_path,
        context_notes_scope=write_context.context_notes_scope,
        context_target_chapter_id=write_context.context_target_chapter_id,
        context_notes_through_chapter_id=write_context.context_notes_through_chapter_id,
    )
    write_translation_store(proj, store)

    console.print(
        f"revised: {record_id} -> {version_ref}" + (" (activated)" if activate else "")
    )
    # Suggest a scoped re-check.
    chapter_id = bundle.index.record_to_chapter.get(record_id, "")
    recheck = check_command(
        proj,
        mode=runtime.mode,
        chapter_id=chapter_id or None,
        fail_on_warnings=True,
    )
    console.print(
        f"recheck: {recheck}",
        soft_wrap=True,
        markup=False,
    )


def translation_revise_block_workflow(
    project_dir: Path,
    file: Path | None = None,
    stdin: bool = False,
    profile: str | None = None,
    output_format: str = "block",
    activate: bool = True,
) -> None:
    """Revise multiple accepted translation records from a block file safely."""
    if output_format != "block":
        _die("translation revise-block currently supports --format block only")
        return
    if (file is None) == (not stdin):
        _die("provide exactly one of --file or --stdin")
        return
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    _require_chunks(proj)
    _require_ready_context(proj)
    from booktx.io_utils import utc_timestamp
    from booktx.submissions import parse_block_submission

    if stdin:
        text = sys.stdin.read()
    else:
        assert file is not None
        if file.is_absolute() or ".." in file.parts:
            _die("--file must be a profile-local relative path")
            return
        file_path = (proj.root / file).resolve()
        root = proj.root.resolve()
        if root not in [file_path, *file_path.parents]:
            _die("--file must stay inside the active profile")
            return
        text = file_path.read_text("utf-8")
    parsed = parse_block_submission(text)
    if not parsed.records:
        _die("block submission contains no records")
        return
    submitted = [
        SubmittedRecord(id=parse_record_ref(r.id).canonical_id, target=r.target)
        for r in parsed.records
    ]
    submitted_ids = {item.id for item in submitted}
    if len(submitted_ids) != len(submitted):
        _die("duplicate record id in block submission")
        return

    store = load_translation_store(proj)
    conflicts = []
    for item in submitted:
        stored = store.records.get(item.id)
        if stored is None:
            _die(f"record {item.id} has no stored translations")
            return
        review = active_review_candidate(stored)
        if review is not None:
            conflicts.append(f"{item.id} ({review.review_ref})")
    if conflicts:
        _die(
            "records have active reviews, so revising active_version will not affect output: "
            + ", ".join(conflicts)
            + ". Use `booktx review deactivate . RECORD` or review correction commands first."
        )
        return

    bundle = _project_status_snapshot(proj)
    context = load_validation_context(proj)
    findings: list[Finding] = []
    source_views: dict[str, SourceRecordView] = {}
    for item in submitted:
        source_view = bundle.index.source_by_id.get(item.id)
        if source_view is None:
            _die(f"record {item.id} has no matching source record")
            return
        source_chunk = bundle.index.source_chunks.get(source_view.chunk_id)
        if source_chunk is None:
            _die(f"record {item.id} has no matching source chunk")
            return
        source_record = next((r for r in source_chunk.records if r.id == item.id), None)
        if source_record is None:
            _die(f"record {item.id} not found in source chunk")
            return
        findings.extend(
            validate_record_pair(
                source_record,
                TranslatedRecord(id=item.id, target=item.target),
                source_chunk.chunk_id,
                context,
            )
        )
        source_views[item.id] = source_view
    errors = [f for f in findings if f.severity == Severity.ERROR]
    if errors:
        _render_submission_failures(errors)
        raise typer.Exit(code=1)
    _staged_preflight_check(proj, submitted, submitted_ids, fail_on_warnings=True)

    write_contexts = _current_write_contexts_for_records(proj, bundle=bundle, record_ids=submitted_ids)
    version_ref = next(iter(write_contexts.values())).version_ref
    for item in submitted:
        source_view = source_views[item.id]
        write_context = write_contexts[bundle.index.record_to_chapter[item.id]]
        ensure_store_record(
            store,
            item.id,
            source=source_view.source,
            source_sha256=source_view.source_sha256,
        )
        upsert_translation_version(
            store.records[item.id],
            version_ref,
            item.target,
            updated_at=utc_timestamp(),
            activate=activate,
            baseline_ref=write_context.baseline_ref,
            baseline_sha256=write_context.baseline_sha256,
            context_view_sha256=write_context.context_view_sha256,
            context_view_path=write_context.context_view_path,
            context_notes_scope=write_context.context_notes_scope,
            context_target_chapter_id=write_context.context_target_chapter_id,
            context_notes_through_chapter_id=write_context.context_notes_through_chapter_id,
        )
    write_translation_store(proj, store)
    chapters = sorted(
        {
            bundle.index.record_to_chapter.get(item.id, "")
            for item in submitted
            if bundle.index.record_to_chapter.get(item.id)
        }
    )
    console.print(
        f"revised: {len(submitted)} record(s) -> {version_ref}"
        + (" (activated)" if activate else "")
    )
    if chapters:
        console.print("affected chapters: " + ", ".join(chapters))
        for chapter_id in chapters:
            console.print(
                "recheck: "
                + check_command(
                    proj,
                    mode=runtime.mode,
                    chapter_id=chapter_id,
                    fail_on_warnings=True,
                ),
                soft_wrap=True,
                markup=False,
            )


def translate_audit_inline_workflow(
    project_dir: Path,
    profile: str | None = None,
    chapter: str | None = None,
    task_id: str | None = None,
    json_output: bool = False,
) -> None:
    """Audit active translations for required EPUB inline XHTML semantics."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    from booktx.inline_audit import audit_inline_xhtml

    result = audit_inline_xhtml(runtime.project, chapter_id=chapter, task_id=task_id)
    if json_output:
        console.print_json(json.dumps(result.as_dict(), ensure_ascii=False))
        return
    console.print("Inline XHTML audit")
    console.print(f"records with inline source: {result.records_with_inline_source}")
    console.print(f"valid active targets: {result.valid_active_targets}")
    console.print(f"missing inline tags: {result.missing_inline_tags}")
    console.print(f"invalid XHTML targets: {result.invalid_xhtml_targets}")
    console.print(f"opaque changed: {result.opaque_changed}")
    console.print(f"needs review: {result.needs_review}")


def translate_migrate_inline_xhtml_workflow(
    project_dir: Path,
    profile: str | None = None,
    dry_run: bool = False,
    write_safe: bool = False,
    json_output: bool = False,
) -> None:
    """Safely migrate legacy targets for simple EPUB inline XHTML cases."""
    if dry_run and write_safe:
        _die("choose either --dry-run or --write-safe")
        return
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    from booktx.inline_audit import migrate_inline_xhtml

    report = migrate_inline_xhtml(runtime.project, write_safe=write_safe)
    if json_output:
        console.print_json(json.dumps(report, ensure_ascii=False))
        return
    console.print("Inline XHTML migration")
    console.print(f"safe mappings: {len(report['mapped_records'])}")
    console.print(f"needs review: {len(report['targets_requiring_review'])}")
    console.print(f"written: {report['written']}")


def translation_search_cmd_workflow(
    project_dir: Path,
    profile: str | None = None,
    target: str | None = None,
    source: str | None = None,
    chapter: str | None = None,
    record: str | None = None,
    before: int = 0,
    after: int = 0,
    jsonl: bool = False,
    *,
    target_regex: str | None = None,
    source_regex: str | None = None,
    exclude_source: str | None = None,
    exclude_source_regex: str | None = None,
    match: str = "any",
    write_block: Path | None = None,
) -> None:
    """Search effective translations without scripting against translation-store.json."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    if match not in {"any", "all"}:
        _die("--match must be 'any' or 'all'")
        return
    import re as _re
    try:
        source_pat = _re.compile(source_regex, _re.IGNORECASE) if source_regex else None
        target_pat = _re.compile(target_regex, _re.IGNORECASE) if target_regex else None
        exclude_source_pat = _re.compile(exclude_source_regex, _re.IGNORECASE) if exclude_source_regex else None
    except _re.error as exc:
        _die(f"invalid regex: {exc}")
        return
    if record is None and not any([source, target, source_pat, target_pat]):
        _die("provide at least one positive search criterion or --record")
        return

    from booktx.config import load_translation_store
    from booktx.translation_store import effective_target_candidate

    bundle = _project_status_snapshot(proj)
    store = load_translation_store(proj)
    store_records = store.records
    source_by_id = bundle.index.source_by_id

    chapters_to_search = (
        [chapter] if chapter is not None else list(bundle.index.record_ids_by_chapter)
    )

    if record is not None:
        stored = store_records.get(record)
        if stored is None:
            _die(f"record {record} not found in store")
            return
        eff = effective_target_candidate(stored)
        source_view = source_by_id.get(record)
        if jsonl:
            import json as _json

            console.print_json(
                _json.dumps(
                    {
                        "id": record,
                        "source": source_view.source if source_view else "",
                        "target": eff.target if eff else "",
                        "effective_ref": (
                            getattr(eff, "review_ref", None)
                            or getattr(eff, "version_ref", None)
                            or ""
                        ),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            console.print(
                f"record: {record}"
                f" chapter={bundle.index.record_to_chapter.get(record, '?')}"
            )
            console.print(f"source: {source_view.source if source_view else ''}")
            console.print(f"target: {eff.target if eff else ''}")
            if eff:
                ref = getattr(eff, "review_ref", None) or getattr(
                    eff, "version_ref", "?"
                )
                console.print(f"ref: {ref}")
        return

    def _neighbor_target(records: dict[str, Any], rid: str) -> str:
        stored = records.get(rid)
        if stored is None:
            return ""
        eff = effective_target_candidate(stored)
        return eff.target if eff is not None else ""

    matches: list[dict[str, object]] = []
    for cid in chapters_to_search:
        flat = list(bundle.index.record_ids_by_chapter.get(cid, []))
        for idx, record_id in enumerate(flat):
            stored = store_records.get(record_id)
            if stored is None:
                continue
            eff = effective_target_candidate(stored)
            if eff is None:
                continue
            source_view = source_by_id.get(record_id)
            source_text = source_view.source if source_view else ""
            target_text = eff.target

            source_hits = []
            target_hits = []
            if source is not None and source.lower() in source_text.lower():
                source_hits.append(source)
            if source_pat is not None and source_pat.search(source_text):
                source_hits.append(source_regex or "")
            if target is not None and target.lower() in target_text.lower():
                target_hits.append(target)
            if target_pat is not None and target_pat.search(target_text):
                target_hits.append(target_regex or "")
            if exclude_source is not None and exclude_source.lower() in source_text.lower():
                continue
            if exclude_source_pat is not None and exclude_source_pat.search(source_text):
                continue
            groups: list[bool] = []
            if source is not None or source_pat is not None:
                groups.append(bool(source_hits))
            if target is not None or target_pat is not None:
                groups.append(bool(target_hits))
            matched = all(groups) if match == "all" else any(groups)

            if matched:
                match_item = {
                    "id": record_id,
                    "chapter_id": cid,
                    "source": source_text if not (target is not None and source is None and source_pat is None) else "",
                    "target": target_text,
                    "effective_ref": (
                        getattr(eff, "review_ref", None)
                        or getattr(eff, "version_ref", None)
                        or ""
                    ),
                    "matched_source": source_hits,
                    "matched_target": target_hits,
                }

                if before > 0 or after > 0:
                    before_ids = flat[max(0, idx - before) : idx]
                    after_ids = flat[idx + 1 : idx + 1 + after]
                    before_records = [
                        {
                            "id": rid,
                            "target": _neighbor_target(store_records, rid),
                        }
                        for rid in before_ids
                    ]
                    after_records = [
                        {
                            "id": rid,
                            "target": _neighbor_target(store_records, rid),
                        }
                        for rid in after_ids
                    ]
                    match_item["before"] = before_records
                    match_item["after"] = after_records

                matches.append(match_item)

    if write_block is not None:
        if write_block.is_absolute() or ".." in write_block.parts:
            _die("--write-block must be a profile-local relative path")
            return
        block_path = (proj.root / write_block).resolve()
        root = proj.root.resolve()
        if root not in [block_path, *block_path.parents]:
            _die("--write-block must stay inside the active profile")
            return
        block_path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        source_lines: list[str] = []
        for item in matches:
            rid = str(item["id"])
            lines.extend([f">>> {rid}", str(item["target"]), ""])
            source_lines.extend([f">>> {rid}", f"source: {item.get('source', '')}", f"target: {item.get('target', '')}", ""])
        block_path.write_text("\n".join(lines).rstrip() + "\n", "utf-8")
        block_path.with_suffix(block_path.suffix + ".sources.txt").write_text("\n".join(source_lines).rstrip() + "\n", "utf-8")
        console.print(f"wrote block: {write_block}")

    if jsonl:
        import json as _json

        for match in matches:
            console.print(
                _json.dumps(match, ensure_ascii=False),
                soft_wrap=True,
                markup=False,
            )
    else:
        console.print(f"found {len(matches)} matches")
        for match in matches:
            rec_id = match.get("id", "")
            target_text = str(match.get("target", ""))
            disp = f"{rec_id}: {target_text[:100]}"
            if len(disp) < len(target_text):
                disp += "..."
            console.print(f"  {disp}", soft_wrap=True, markup=False)
