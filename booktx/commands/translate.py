# ruff: noqa: E501
"""Typer commands for the translation workflow (Phase 3 slice 7).

Thin command layer for the ``translate`` group (registered twice as
``translate`` and the ``translation`` alias). Each command parses options and
delegates to :mod:`booktx.workflows.translate`. The alias equality is
preserved because both names register the same ``translate_app`` object.
"""

from __future__ import annotations

from pathlib import Path

import typer

from booktx.workflows.translate import (
    translate_audit_inline_workflow,
    translate_export_index_workflow,
    translate_export_workflow,
    translate_import_legacy_workflow,
    translate_insert_workflow,
    translate_migrate_inline_xhtml_workflow,
    translate_migrate_store_workflow,
    translate_next_workflow,
    translate_set_record_workflow,
    translate_task_status_workflow,
    translate_todo_next_workflow,
    translate_todo_resume_workflow,
    translate_todo_status_workflow,
    translation_activate_workflow,
    translation_compare_workflow,
    translation_get_record_workflow,
    translation_list_workflow,
    translation_review_workflow,
    translation_revise_block_workflow,
    translation_revise_record_workflow,
    translation_search_cmd_workflow,
)

translate_app = typer.Typer(help="Command-based translation workflow.")


@translate_app.command(name="next")
def translate_next(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    chapter: str | None = typer.Option(None, "--chapter", help="Optional chapter id."),
    unit: str = typer.Option(
        "paragraph",
        "--unit",
        help="Work-unit selection: paragraph, batch, chunk, or chapter.",
    ),
    max_words: int = typer.Option(
        900,
        "--max-words",
        help="Maximum source words to return for paragraph or batch work units.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
    output_format: str = typer.Option(
        "text",
        "--format",
        help="Human output format: text, tsv, or block.",
    ),
    show_sources: bool = typer.Option(
        False,
        "--show-sources",
        help="Print source records inline (block format only).",
    ),
    show_template: bool = typer.Option(
        False,
        "--show-template",
        help="Print the heredoc submit template inline (block format only).",
    ),
    allow_missing_context: bool = typer.Option(
        False,
        "--allow-missing-context",
        help="Legacy override: allow next without a ready translation context.",
    ),
    chapter_word_limit: int | None = typer.Option(
        None,
        "--chapter-word-limit",
        help="Source-word threshold above which --unit chapter is treated as oversized.",
    ),
    large_chapter_mode: str = typer.Option(
        "todo",
        "--large-chapter-mode",
        help="How to handle oversized --unit chapter requests: todo, error, or chapter.",
    ),
    force_chapter: bool = typer.Option(
        False,
        "--force-chapter",
        help="Force --unit chapter regardless of size (alias for --large-chapter-mode chapter).",
    ),
) -> None:
    """"""
    translate_next_workflow(
        project_dir,
        profile,
        chapter,
        unit,
        max_words,
        as_json,
        output_format,
        show_sources,
        show_template,
        allow_missing_context,
        chapter_word_limit,
        large_chapter_mode,
        force_chapter,
    )


@translate_app.command(name="insert")
def translate_insert(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    task_id: str | None = typer.Option(None, "--task-id", help="Optional task id."),
    stdin: bool = typer.Option(False, "--stdin", help="Read the payload from stdin."),
    record_id: str | None = typer.Option(None, "--record-id", help="Single record id."),
    target: str | None = typer.Option(None, "--target", help="Single target text."),
    json_file: Path | None = typer.Option(
        None,
        "--json-file",
        help="Compatibility sugar for --format json --file PATH.",
    ),
    input_file: Path | None = typer.Option(
        None,
        "--file",
        help="Read submission payload from a file using --format.",
    ),
    input_format: str = typer.Option(
        "json",
        "--format",
        help="Input format for --stdin/--file payloads: json, tsv, or block.",
    ),
    allow_missing_context: bool = typer.Option(
        False,
        "--allow-missing-context",
        help="Legacy override: allow insert without a ready translation context.",
    ),
    export_index: bool = typer.Option(
        False, "--export-index", help="Export editor QA indexes after acceptance."
    ),
) -> None:
    """"""
    translate_insert_workflow(
        project_dir,
        profile,
        task_id,
        stdin,
        record_id,
        target,
        json_file,
        input_file,
        input_format,
        allow_missing_context,
        export_index,
    )


@translate_app.command(name="todo-next")
def translate_todo_next(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    chapters: int = typer.Option(
        3, "--chapters", min=1, help="Number of incomplete chapters to complete."
    ),
    batch_words: int = typer.Option(
        800, "--batch-words", min=1, help="Source-word budget per translate next batch."
    ),
    max_run_words: int | None = typer.Option(
        None,
        "--max-run-words",
        min=1,
        help="Optional source-word cap for this agent run.",
    ),
    start_chapter: str | None = typer.Option(
        None,
        "--start-chapter",
        help="Optional chapter id to start from.",
    ),
    skip_current: bool = typer.Option(
        False,
        "--skip-current",
        help="Start after the current first incomplete chapter.",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Write todo markdown/json under translations/<profile>/todos/.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """"""
    translate_todo_next_workflow(
        project_dir,
        profile,
        chapters,
        batch_words,
        max_run_words,
        start_chapter,
        skip_current,
        write,
        as_json,
    )


@translate_app.command(name="todo-status")
def translate_todo_status(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    todo_id: str | None = typer.Option(None, "--todo-id", help="Todo id to inspect."),
    latest: bool = typer.Option(
        False, "--latest", help="Select the latest incomplete todo."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit stable JSON output."),
) -> None:
    """"""
    translate_todo_status_workflow(
        project_dir,
        profile,
        todo_id,
        latest,
        as_json,
    )


@translate_app.command(name="todo-resume")
def translate_todo_resume(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    todo_id: str | None = typer.Option(None, "--todo-id", help="Todo id to resume."),
    latest: bool = typer.Option(
        False, "--latest", help="Resume the latest incomplete todo."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
    output_format: str = typer.Option(
        "block",
        "--format",
        help="Human output format: text, tsv, or block.",
    ),
    show_sources: bool = typer.Option(
        False,
        "--show-sources",
        help="Print source records inline (block format only).",
    ),
    show_template: bool = typer.Option(
        False,
        "--show-template",
        help="Print the heredoc submit template inline (block format only).",
    ),
) -> None:
    """"""
    translate_todo_resume_workflow(
        project_dir,
        profile,
        todo_id,
        latest,
        as_json,
        output_format,
        show_sources,
        show_template,
    )


@translate_app.command(name="import-legacy")
def translate_import_legacy(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """"""
    translate_import_legacy_workflow(
        project_dir,
        profile,
    )


@translate_app.command(name="migrate-store")
def translate_migrate_store(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    write: bool = typer.Option(False, "--write", help="Rewrite the store as v2."),
    actor: str | None = typer.Option(
        None, "--actor", help="Actor for migrated ledger."
    ),
    harness: str | None = typer.Option(
        None, "--harness", help="Harness for migrated ledger."
    ),
    model: str | None = typer.Option(
        None, "--model", help="Model for migrated ledger."
    ),
    context_label: str | None = typer.Option(
        None,
        "--context-label",
        help="Optional label stored on the migrated subversion.",
    ),
    allow_missing_source: bool = typer.Option(
        False,
        "--allow-missing-source",
        help="Write migrated records even when some legacy ids no longer exist in source chunks.",
    ),
) -> None:
    """"""
    translate_migrate_store_workflow(
        project_dir,
        profile,
        write,
        actor,
        harness,
        model,
        context_label,
        allow_missing_source,
    )


@translate_app.command(name="export")
def translate_export(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    version_ref: str | None = typer.Option(
        None, "--version", help="Export one exact version ref such as 1.2."
    ),
    track: int | None = typer.Option(
        None,
        "--track",
        help="Export one major track, optionally with --latest-subversion.",
    ),
    latest_subversion: bool = typer.Option(
        False,
        "--latest-subversion",
        help="When exporting a track, choose the latest accepted subversion per record.",
    ),
    all_versions: bool = typer.Option(
        False,
        "--all-versions",
        help="Export all accepted versions into translated/<version-ref>/ chunk files.",
    ),
) -> None:
    """"""
    translate_export_workflow(
        project_dir,
        profile,
        version_ref,
        track,
        latest_subversion,
        all_versions,
    )


@translate_app.command(name="export-index")
def translate_export_index(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    kind: list[str] = typer.Option(
        [],
        "--kind",
        help=(
            "Index kind to write. Repeatable. One of source, target, "
            "source-target. Defaults to all three kinds."
        ),
    ),
    fail_on_warn: bool = typer.Option(
        False,
        "--fail-on-warn",
        help="Fail when target validation warnings are present.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit command summary as JSON."),
    jsonl: bool = typer.Option(
        False,
        "--jsonl",
        help="Also write current-only JSONL aliases next to the JSON indexes.",
    ),
) -> None:
    """"""
    translate_export_index_workflow(
        project_dir,
        profile,
        kind,
        fail_on_warn,
        as_json,
        jsonl,
    )


@translate_app.command(name="task-status")
def translate_task_status(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    task_id: str = typer.Option(..., "--task-id", help="Task id to inspect."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """"""
    translate_task_status_workflow(
        project_dir,
        task_id,
        profile,
        as_json,
    )


@translate_app.command(name="get-record")
def translation_get_record(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    record_ref: str = typer.Argument(..., help="Record ref such as 74@38."),
    before: int = typer.Option(0, "--before", min=0, help="Neighbor records before."),
    after: int = typer.Option(0, "--after", min=0, help="Neighbor records after."),
    version: str | None = typer.Option(
        None, "--version", help="Show one specific version."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """"""
    translation_get_record_workflow(
        project_dir,
        record_ref,
        before,
        after,
        version,
        profile,
        as_json,
    )


@translate_app.command(name="list")
def translation_list(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    range_spec: str | None = typer.Option(
        None, "--range", help="Range spec such as 74@38..74@42."
    ),
    chapter: int | None = typer.Option(None, "--chapter", help="Chapter number."),
    version: str | None = typer.Option(
        None, "--version", help="Show a specific version."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """"""
    translation_list_workflow(
        project_dir,
        range_spec,
        chapter,
        version,
        profile,
        as_json,
    )


@translate_app.command(name="compare")
def translation_compare(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    record_ref: str = typer.Argument(..., help="Record ref such as 74@38."),
    versions: str = typer.Option(
        ..., "--versions", help="Comma-separated version refs."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """"""
    translation_compare_workflow(
        project_dir,
        record_ref,
        versions,
        profile,
        as_json,
    )


@translate_app.command(name="activate")
def translation_activate(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    record_ref: str = typer.Argument(..., help="Record ref such as 74@38."),
    version_ref: str = typer.Argument(..., help="Version ref to activate."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """"""
    translation_activate_workflow(
        project_dir,
        record_ref,
        version_ref,
        profile,
    )


@translate_app.command(name="review")
def translation_review(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    record_ref: str = typer.Argument(..., help="Record ref such as 74@38."),
    activate: str | None = typer.Option(
        None, "--activate", help="Optionally activate a version."
    ),
    note: str | None = typer.Option(None, "--note", help="Review note."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """"""
    translation_review_workflow(
        project_dir,
        record_ref,
        activate,
        note,
        profile,
    )


@translate_app.command(name="set-record")
def translate_set_record(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    task_id: str = typer.Option(..., "--task-id", help="Task id owning the record."),
    record_id: str = typer.Option(..., "--record-id", help="Record id to set."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    stdin: bool = typer.Option(
        False,
        "--stdin",
        help="Read the target text from stdin (default source).",
    ),
    target: str | None = typer.Option(None, "--target", help="Inline target text."),
    allow_missing_context: bool = typer.Option(
        False,
        "--allow-missing-context",
        help="Legacy override: allow set-record without a ready context.",
    ),
) -> None:
    """"""
    translate_set_record_workflow(
        project_dir,
        task_id,
        record_id,
        profile,
        stdin,
        target,
        allow_missing_context,
    )


@translate_app.command(name="revise-record")
def translation_revise_record(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    record_ref: str = typer.Argument(..., help="Record id to revise."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    stdin: bool = typer.Option(
        False,
        "--stdin",
        help="Read the target text from stdin (default source).",
    ),
    target: str | None = typer.Option(None, "--target", help="Inline target text."),
    activate: bool = typer.Option(
        True,
        "--activate/--no-activate",
        help="Activate the revised version after writing.",
    ),
) -> None:
    """"""
    translation_revise_record_workflow(
        project_dir,
        record_ref,
        profile,
        stdin,
        target,
        activate,
    )


@translate_app.command(name="revise-block")
def translation_revise_block(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    file: Path | None = typer.Option(None, "--file", help="Block submission file."),
    stdin: bool = typer.Option(
        False, "--stdin", help="Read block submission from stdin."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    output_format: str = typer.Option(
        "block", "--format", help="Submission format: block."
    ),
    activate: bool = typer.Option(
        True,
        "--activate/--no-activate",
        help="Activate revised versions after writing.",
    ),
) -> None:
    """"""
    translation_revise_block_workflow(
        project_dir,
        file,
        stdin,
        profile,
        output_format,
        activate,
    )


@translate_app.command(name="audit-inline")
def translate_audit_inline(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    chapter: str | None = typer.Option(
        None, "--chapter", help="Scope to a specific chapter id."
    ),
    task_id: str | None = typer.Option(
        None, "--task-id", help="Scope to a specific task id."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """"""
    translate_audit_inline_workflow(
        project_dir,
        profile,
        chapter,
        task_id,
        json_output,
    )


@translate_app.command(name="migrate-inline-xhtml")
def translate_migrate_inline_xhtml(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report safe migrations without writing translated chunks.",
    ),
    write_safe: bool = typer.Option(
        False, "--write-safe", help="Write only safe automatic migrations."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """"""
    translate_migrate_inline_xhtml_workflow(
        project_dir,
        profile,
        dry_run,
        write_safe,
        json_output,
    )


@translate_app.command(name="search")
def translation_search_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    target: str | None = typer.Option(
        None, "--target", help="Literal text to find in effective targets."
    ),
    source: str | None = typer.Option(
        None, "--source", help="Literal text to find in source text."
    ),
    target_regex: str | None = typer.Option(None, "--target-regex", help="Regex to find in effective targets."),
    source_regex: str | None = typer.Option(None, "--source-regex", help="Regex to find in source text."),
    exclude_source: str | None = typer.Option(None, "--exclude-source", help="Reject records containing this source literal."),
    exclude_source_regex: str | None = typer.Option(None, "--exclude-source-regex", help="Reject records matching this source regex."),
    match: str = typer.Option("any", "--match", help="Positive group match semantics: any or all."),
    write_block: Path | None = typer.Option(None, "--write-block", help="Write editable correction block."),
    chapter: str | None = typer.Option(
        None, "--chapter", help="Scope to one chapter id."
    ),
    record: str | None = typer.Option(
        None, "--record", help="Show one specific record id."
    ),
    before: int = typer.Option(0, "--before", help="Context records before the match."),
    after: int = typer.Option(0, "--after", help="Context records after the match."),
    jsonl: bool = typer.Option(
        False, "--jsonl", help="Output one JSON object per match per line."
    ),
) -> None:
    """"""
    translation_search_cmd_workflow(
        project_dir,
        profile,
        target,
        source,
        chapter,
        record,
        before,
        after,
        jsonl,
        target_regex=target_regex,
        source_regex=source_regex,
        exclude_source=exclude_source,
        exclude_source_regex=exclude_source_regex,
        match=match,
        write_block=write_block,
    )
