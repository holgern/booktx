"""Rich/text/JSON output renderers.

Moved out of :mod:`booktx.cli` so command functions contain only option
parsing + service call + renderer call. Each renderer owns the exact console
output and can be unit-tested without Typer.

The module creates its own :class:`rich.console.Console` so it never depends
on the CLI module (which would create a circular import).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from rich.console import Console

from booktx.tasks import task_paths

if TYPE_CHECKING:
    from booktx.config import Project
    from booktx.models import TranslationTask
    from booktx.status import ChapterProgress, StatusBundle
    from booktx.validate import Finding

__all__ = [
    "console",
    "format_chunk_span",
    "print_status_human",
    "print_translate_task",
    "render_submission_failures",
]

console = Console()


def format_chunk_span(chunk_ids: list[str]) -> str:
    """Return a compact chunk-id range string for human display."""
    if not chunk_ids:
        return "-"
    if len(chunk_ids) == 1:
        return chunk_ids[0]
    return f"{chunk_ids[0]}..{chunk_ids[-1]}"


def print_status_human(bundle: StatusBundle, chapter: ChapterProgress | None) -> None:
    """Render the human-readable ``booktx status`` summary to the console."""
    snapshot = bundle.snapshot
    totals = snapshot.totals
    source = snapshot.source
    ctx = snapshot.context
    console.print(f"booktx status — {snapshot.project}")
    console.print()
    console.print(f"Source: {source.filename}")
    console.print(f"Source language: {source.source_language}")
    console.print(f"Target language: {source.target_language}")
    console.print(f"Context: {'READY' if ctx.ready else 'NOT READY'}")
    if source.source_drifted:
        console.print(
            "[yellow]WARNING: source file changed since last extraction.[/yellow]"
        )
        console.print("  Run 'booktx extract' to update chunks before translating.")
    console.print()
    console.print(f"Total source words: {totals.source_words:>10,}")
    console.print(f"Translated words:   {totals.translated_words:>10,}")
    console.print(f"Remaining words:    {totals.remaining_words:>10,}")
    console.print()
    console.print(
        f"Chunks:   {totals.chunks_complete} / {totals.chunks_total} complete, "
        f"{totals.chunks_partial} partial, {totals.chunks_pending} pending"
    )
    console.print(
        f"Chapters: {totals.chapters_complete} / "
        f"{totals.chapters_total} complete, "
        f"{totals.chapters_partial} partial, {totals.chapters_pending} pending"
    )
    if totals.invalid_translation_files or totals.stale_translation_files:
        console.print(
            f"Translation files: {totals.invalid_translation_files} invalid, "
            f"{totals.stale_translation_files} stale"
        )
    ready_for_final = (
        totals.records_remaining == 0 and totals.invalid_translation_files == 0
    )
    console.print()
    console.print(f"Ready for final build: {'yes' if ready_for_final else 'no'}")
    if not ready_for_final:
        if totals.remaining_words > 0:
            console.print(
                f"Reason: {totals.remaining_words:,} source words remain untranslated"
            )
        elif totals.invalid_translation_files > 0:
            console.print(
                "Reason: "
                f"{totals.invalid_translation_files} translation file(s) "
                "are invalid"
            )
    detail = chapter or snapshot.next
    if detail is None:
        return
    console.print()
    console.print("Next chapter:" if chapter is None else "Chapter:")
    console.print(f"  {detail.chapter_id}  {detail.title}".rstrip())
    console.print(f"  status: {detail.status}")
    console.print(
        f"  records: {detail.records_translated} / "
        f"{detail.records_total} translated, "
        f"{detail.records_remaining} remaining"
    )
    console.print(
        f"  words: {detail.source_words_translated:,} / "
        f"{detail.source_words_total:,} translated, "
        f"{detail.source_words_remaining:,} remaining"
    )
    console.print(f"  chunks: {format_chunk_span(detail.chunk_ids)}")
    console.print(f"  pending chunks: {format_chunk_span(detail.pending_chunk_ids)}")
    console.print(
        f"  record range: {detail.record_range.start}..{detail.record_range.end}"
    )


def print_translate_task(
    task: TranslationTask,
    project: Project,
    *,
    as_json: bool,
    output_format: str,
    show_sources: bool = False,
    show_template: bool = False,
) -> None:
    """Render a ``translate next`` / ``translate task`` result to the console.

    Supports four output modes: ``--json``, ``--format tsv``, ``--format block``
    (the durable agent workflow), and the default human-readable list.
    """
    paths = task_paths(project, task.task_id)
    display = paths.display(project.root)
    json_submit = paths.json_submit_hint(task.task_id, project.root)
    block_submit = paths.block_submit_hint(task.task_id, project.root)
    block_stdin = paths.block_stdin_submit_hint(task.task_id)
    view_sources = f"cat {display.source_block}"

    payload = {
        "version": 1,
        "task_id": task.task_id,
        "unit": task.unit,
        "chapter_id": task.chapter_id,
        "chapter_title": task.chapter_title,
        "source_language": task.source_language,
        "target_language": task.target_language,
        "translation_version": task.translation_version,
        "context_sha256": task.context_sha256,
        "source_sha256": task.source_sha256,
        "source_words": task.source_words,
        "record_count": task.record_count,
        "records": [record.model_dump(mode="json") for record in task.records],
        "ingest_path": display.ingest_json,
        "block_ingest_path": display.ingest_block,
        "source_block_path": display.source_block,
        "submit_hint": json_submit,
        "block_submit_hint": block_submit,
    }

    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    if output_format == "tsv":
        console.print(f"# task: {task.task_id}")
        console.print(f"# chapter: {task.chapter_id}\t{task.chapter_title}".rstrip())
        for record in task.records:
            console.print(f"{record.id}\t{record.source}")
        console.print(f"# write translation JSON to: {display.ingest_json}")
        console.print(f"# submit: {json_submit}")
        return
    if output_format == "block":
        console.print(f"task: {task.task_id}")
        console.print(f"chapter: {task.chapter_id}  {task.chapter_title}".rstrip())
        console.print(f"unit: {task.unit}")
        console.print(f"records: {task.record_count}")
        console.print(f"source words: {task.source_words}")
        console.print()
        console.print(f"Source file: {display.source_block}", soft_wrap=True)
        console.print(f"Durable block template: {display.ingest_block}", soft_wrap=True)
        console.print(f"Submit durable file with: {block_submit}", soft_wrap=True)
        console.print(f"View sources: {view_sources}", soft_wrap=True)
        if show_template:
            console.print()
            console.print("Heredoc template (optional, for tiny manual fixes):")
            console.print()
            console.print(block_stdin, soft_wrap=True)
            for idx, record in enumerate(task.records):
                console.print(f">>> {record.id}")
                console.print("<target>")
                if idx != len(task.records) - 1:
                    console.print()
            console.print("BOOKTX")
        if show_sources:
            console.print()
            console.print("Sources:")
            console.print()
            for idx, record in enumerate(task.records):
                console.print(f">>> {record.id}")
                console.print(record.source)
                if idx != len(task.records) - 1:
                    console.print()
        return
    # Default: human-readable list.
    console.print(f"task: {task.task_id}")
    console.print(f"chapter: {task.chapter_id}  {task.chapter_title}".rstrip())
    console.print(f"unit: {task.unit}")
    console.print(f"records: {task.record_count}")
    console.print(f"source words: {task.source_words}")
    console.print()
    for idx, record in enumerate(task.records):
        if idx:
            console.print()
        console.print(record.id)
        console.print(record.source)
    console.print()
    console.print("Write translation JSON to:")
    console.print(display.ingest_json)
    console.print("Submit with:")
    console.print(json_submit)


def render_submission_failures(findings: list[Finding]) -> None:
    """Render submission validation ERROR findings to the console."""
    console.print("[red]error:[/red] submission rejected; no files changed")
    console.print()
    for finding in findings:
        if finding.record_id:
            console.print(f"{finding.record_id} {finding.rule}:")
        else:
            console.print(f"{finding.chunk_id} {finding.rule}:")
        console.print(f"  {finding.message}")
