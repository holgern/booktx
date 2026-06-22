"""Typer CLI for booktx.

Commands (see ``booktx_coding_agent_start.md``)::

    booktx init ./book --target de
    booktx inspect ./book
    booktx extract ./book
    booktx next ./book
    booktx validate ./book
    booktx build ./book

booktx never translates text; it extracts, validates, and rebuilds.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from booktx import __version__
from booktx.build import BuildError, build_project
from booktx.chapters import detect_chapters, load_chapter_map, write_chapter_map
from booktx.chunking import spans_to_chunks
from booktx.config import (
    BooktxError,
    find_source_file,
    init_project,
    load_manifest,
    load_project,
    load_translation_store,
    load_translation_task,
    project_source_sha256,
    translation_ingest_block_path,
    translation_ingest_path,
    translation_task_path,
    translation_task_source_block_path,
    write_translation_store,
    write_translation_task,
)
from booktx.context import (
    GlossaryEntry,
    apply_answer_to_context,
    context_markdown_path,
    default_context,
    load_context,
    write_context,
    write_context_markdown,
)
from booktx.epub_io import extract_epub
from booktx.epub_manifest import EPUB2TEXT_SCHEMA, EPUB_TEMPLATE_PIPELINE
from booktx.html_io import build_xhtml  # noqa: F401  (kept for downstream use)
from booktx.markdown_io import extract_markdown
from booktx.models import (
    NamesFile,
    StoredTranslationRecord,
    TranslatedChunk,
    TranslatedRecord,
    TranslationTask,
    TranslationTaskRecord,
)
from booktx.progress import (
    count_words,
    load_source_chunks,
    load_source_records,
    source_record_sha256,
)
from booktx.validate import (
    Severity,
    load_effective_translated_chunks,
    strict_load_translated,
    validate_chunk_pair,
    validate_project,
    validate_record_pair,
    write_report,
)

app = typer.Typer(
    name="booktx",
    help=(
        "Prepare Markdown and EPUB documents for translation by a coding agent. "
        "booktx does NOT translate text; it extracts, validates, and rebuilds."
    ),
    invoke_without_command=True,
    add_completion=False,
)

console = Console()
context_app = typer.Typer(help="Build, inspect, and render translation context.")
translate_app = typer.Typer(help="Command-based translation workflow.")
app.add_typer(context_app, name="context")
app.add_typer(translate_app, name="translate")


def _die(message: str, code: int = 1) -> None:
    """Print an error and exit with ``code``."""
    console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(code=code)


def _read_submission_file_or_die(path: Path) -> str:
    """Read a submission file, dying with a concise CLI error on failure.

    Missing/unreadable files produce a short error message (never a Python
    traceback). When the path looks like it lives outside ``.booktx/ingest``
    we add a hint pointing the agent at the generated durable ingest file,
    since that is the recommended submission location.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        message = f"submission file not found: {path}"
    except PermissionError:
        message = f"submission file is not readable: {path}"
    except OSError as exc:
        message = f"could not read submission file {path}: {exc}"
    resolved = path.expanduser().resolve()
    parts = resolved.parts
    ingest_parts = (".booktx", "ingest")
    looks_outside_ingest = bool(parts) and not any(
        parts[i : i + 2] == ingest_parts for i in range(len(parts) - 1)
    )
    if looks_outside_ingest:
        message += (
            "\nhint: use the generated .booktx/ingest/<task>.block.txt file "
            "instead of /tmp or other temporary locations"
        )
    _die(message)


def _handle_booktx_error(exc: BooktxError) -> None:
    _die(str(exc))


# --- version -----------------------------------------------------------------


@app.command()
def version() -> None:
    """Print the booktx version."""
    console.print(__version__)


# --- context -----------------------------------------------------------------


def _load_project_or_exit(project_dir: Path):
    try:
        return load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        raise typer.Exit(code=1) from exc


def _load_context_or_exit(proj):
    try:
        ctx = load_context(proj)
    except Exception as exc:  # noqa: BLE001 - surface as user-facing CLI error
        _die(f"translation context is invalid: {exc}")
        raise typer.Exit(code=1) from exc
    if ctx is None:
        _die("translation context is missing. Run: booktx context init .")
    return ctx


def _open_required_questions(ctx) -> list:
    return [q for q in ctx.questions if q.required and q.status == "open"]


@context_app.command(name="init")
def context_init(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    non_interactive: bool = typer.Option(
        True, "--non-interactive/--interactive", help="Create open questions or prompt."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing context."),
) -> None:
    """Create .booktx/context.json and rendered context.md."""
    proj = _load_project_or_exit(project_dir)
    existing = None if force else load_context(proj)
    if existing is not None:
        write_context_markdown(proj, existing)
        console.print(f"context exists: {context_markdown_path(proj)}")
        return

    ctx = default_context(proj)
    if not non_interactive:
        for q in ctx.questions:
            answer = typer.prompt(q.question, default="", show_default=False)
            if answer.strip():
                q.answer = answer.strip()
                q.status = "answered"
        ctx.ready = not _open_required_questions(ctx)
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    console.print(f"wrote {proj.booktx_dir / 'context.json'}")
    console.print(f"wrote {context_markdown_path(proj)}")


@context_app.command(name="questions")
def context_questions(
    project_dir: Path = typer.Argument(..., help="Project directory."),
) -> None:
    """List context questions."""
    proj = _load_project_or_exit(project_dir)
    ctx = _load_context_or_exit(proj)
    for q in ctx.questions:
        marker = "required" if q.required else "optional"
        answer = f" -> {q.answer}" if q.answer else ""
        console.print(f"{q.id} [{marker}] {q.status} {q.topic}: {q.question}{answer}")


@context_app.command(name="status")
def context_status(
    project_dir: Path = typer.Argument(..., help="Project directory."),
) -> None:
    """Show translation context readiness."""
    proj = _load_project_or_exit(project_dir)
    ctx = _load_context_or_exit(proj)
    open_required = _open_required_questions(ctx)
    open_total = [q for q in ctx.questions if q.status == "open"]
    status = "READY" if ctx.ready else "NOT READY"
    console.print(f"Status: {status}")
    console.print(f"open_required={len(open_required)} open_total={len(open_total)}")
    console.print(f"glossary_entries={len(ctx.glossary)}")
    console.print(f"context: {context_markdown_path(proj)}", soft_wrap=True)


@context_app.command(name="render")
def context_render(
    project_dir: Path = typer.Argument(..., help="Project directory."),
) -> None:
    """Render context.md from context.json."""
    proj = _load_project_or_exit(project_dir)
    ctx = _load_context_or_exit(proj)
    write_context_markdown(proj, ctx)
    console.print(f"rendered {context_markdown_path(proj)}")


@context_app.command(name="answer")
def context_answer(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    question_id: str = typer.Argument(..., help="Question id, e.g. Q001."),
    text: str = typer.Option(..., "--text", help="Answer text."),
) -> None:
    """Answer one context question non-interactively."""
    proj = _load_project_or_exit(project_dir)
    ctx = _load_context_or_exit(proj)
    for q in ctx.questions:
        if q.id == question_id:
            q.answer = text
            q.status = "answered" if text.strip() else "open"
            apply_answer_to_context(ctx, question_id, text)
            write_context(proj, ctx)
            write_context_markdown(proj, ctx)
            console.print(f"answered {question_id}")
            return
    _die(f"unknown question id: {question_id}")


@context_app.command(name="add-term")
def context_add_term(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    source: str = typer.Argument(..., help="Source term."),
    target: str | None = typer.Option(None, "--target", help="Approved target term."),
    forbid: list[str] | None = typer.Option(
        None, "--forbid", help="Forbidden target term (repeatable)."
    ),
    category: str = typer.Option("term", "--category", help="Glossary category."),
    notes: str = typer.Option("", "--notes", help="Glossary notes."),
    enforce: str = typer.Option(
        "warn", "--enforce", help="Enforcement: off, warn, or error."
    ),
) -> None:
    """Add or update a glossary entry."""
    if enforce not in {"off", "warn", "error"}:
        _die("--enforce must be off, warn, or error")
    proj = _load_project_or_exit(project_dir)
    ctx = _load_context_or_exit(proj)
    forbidden = forbid or []
    for entry in ctx.glossary:
        if entry.source == source:
            if target is not None:
                entry.target = target
                entry.status = "approved" if target else entry.status
            for value in forbidden:
                if value not in entry.forbidden_targets:
                    entry.forbidden_targets.append(value)
            entry.category = category or entry.category
            entry.notes = notes or entry.notes
            entry.enforce = enforce  # type: ignore[assignment]
            break
    else:
        ctx.glossary.append(
            GlossaryEntry(
                source=source,
                target=target,
                forbidden_targets=forbidden,
                category=category,
                status="approved" if target else "open",
                notes=notes,
                enforce=enforce,  # type: ignore[arg-type]
            )
        )
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    console.print(f"updated term: {source}")


@context_app.command(name="mark-ready")
def context_mark_ready(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    force: bool = typer.Option(
        False, "--force", help="Mark ready even with open required questions."
    ),
) -> None:
    """Mark context ready once required questions are answered."""
    proj = _load_project_or_exit(project_dir)
    ctx = _load_context_or_exit(proj)
    open_required = _open_required_questions(ctx)
    if open_required and not force:
        ids = ", ".join(q.id for q in open_required)
        _die(f"required questions are still open: {ids}")
    ctx.ready = True
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    console.print(f"context ready: {context_markdown_path(proj)}")


# --- init --------------------------------------------------------------------


@app.command()
def init(
    project_dir: Path = typer.Argument(..., help="Directory to create the project in."),
    target: str = typer.Option(
        ..., "--target", "-t", help="Target language code, e.g. de."
    ),
    source_lang: str = typer.Option(
        "en", "--source", "-s", help="Source language code (default: en)."
    ),
    source: Path | None = typer.Option(
        None,
        "--source-file",
        help="Optional source document to copy into <project>/source/.",
    ),
    chunk_size: int = typer.Option(
        50, "--chunk-size", help="Max records per chunk (default: 50)."
    ),
) -> None:
    """Create a new booktx project layout."""
    try:
        proj = init_project(
            project_dir,
            target_language=target,
            source_language=source_lang,
            source_file=source,
            chunk_size=chunk_size,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return

    console.print(f"[green]Created project:[/green] {proj.root}")
    console.print(f"  source_language: {proj.config.source_language}")
    console.print(f"  target_language: {proj.config.target_language}")
    console.print(f"  format:          {proj.config.format}")
    if proj.config.source_file:
        console.print(f"  source_file:     {proj.config.source_file}")
    else:
        console.print(
            "  [yellow]source/ is empty — drop a .md or .epub file into it.[/yellow]"
        )


# --- inspect -----------------------------------------------------------------


@app.command()
def inspect(
    project_dir: Path = typer.Argument(..., help="Project directory."),
) -> None:
    """Summarise the source document and how many records it would yield."""
    try:
        proj = load_project(project_dir)
        source = find_source_file(proj)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return

    fmt = proj.config.format
    names = _load_names_list(proj)
    record_count, extra = _count_records(
        source, fmt, names, proj.config.source_language
    )

    table = Table(title=f"booktx inspect — {proj.root}", show_header=False)
    table.add_row("source_file", source.name)
    table.add_row("format", fmt)
    table.add_row("source_language", proj.config.source_language)
    table.add_row("target_language", proj.config.target_language)
    table.add_row("estimated_records", str(record_count))
    table.add_row("protected_terms", ", ".join(names) if names else "(none)")
    table.add_row("details", extra)
    console.print(table)


def _load_names_list(proj) -> list[str]:
    from booktx.config import load_names

    return load_names(proj).protected_terms


def _count_records(
    source: Path, fmt: str, names: list[str], source_language: str
) -> tuple[int, str]:
    if fmt == "markdown":
        text = source.read_text("utf-8")
        ext = extract_markdown(text, protected_terms=names)
        spans = ext.spans
        details = f"{len(spans)} prose span(s)"
    elif fmt == "epub":
        extraction = extract_epub(str(source), protected_terms=names)
        spans = extraction.spans
        entries = extraction.text2epub_manifest.get("entries", [])
        block_entries = [entry for entry in entries if entry.get("blocks")]
        details = f"{len(block_entries)} spine document(s) with text blocks"
    else:  # pragma: no cover - config validation already guards this
        raise BooktxError(f"Unsupported format {fmt!r}")

    from booktx.chunking import segment_spans

    records = segment_spans(spans, language=source_language)
    return len(records), details


# --- extract -----------------------------------------------------------------


@app.command()
def extract(
    project_dir: Path = typer.Argument(..., help="Project directory."),
) -> None:
    """Extract translatable chunks into ``.booktx/chunks/``.

    Idempotent: ``chunks/`` is rebuilt each run; ``translated/`` is left intact.
    """
    try:
        proj = load_project(project_dir)
        source = find_source_file(proj)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return

    names = _load_names_list(proj)
    fmt = proj.config.format
    if fmt == "markdown":
        text = source.read_text("utf-8")
        ext = extract_markdown(text, protected_terms=names)
        spans = ext.spans
    elif fmt == "epub":
        extraction = extract_epub(str(source), protected_terms=names)
        spans = extraction.spans
    else:  # pragma: no cover
        _die(f"Unsupported format {fmt!r}")
        return

    chunks = spans_to_chunks(
        spans,
        source_language=proj.config.source_language,
        target_language=proj.config.target_language,
        chunk_size=proj.config.chunk_size,
    )
    if fmt == "epub":
        _assert_epub_records_are_clean(chunks)

    # Idempotent rebuild of chunks/ — wipe and rewrite, keep translated/.
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    for old in proj.chunks_dir.glob("*.json"):
        old.unlink()
    for chunk in chunks:
        (proj.chunks_dir / f"{chunk.chunk_id}.json").write_text(
            chunk.model_dump_json(indent=2), encoding="utf-8"
        )

    record_count = sum(len(c.records) for c in chunks)
    if fmt == "epub":
        _save_epub_manifest(proj, source, extraction, len(chunks), record_count)
    console.print(
        f"[green]Extracted[/green] {len(chunks)} chunk(s), "
        f"{record_count} record(s) into {proj.chunks_dir}"
    )


def _assert_epub_records_are_clean(chunks) -> None:
    for chunk in chunks:
        for record in chunk.records:
            if "__TAG_" in record.source or "__SPANTX_" in record.source:
                raise BooktxError(
                    "new EPUB extraction produced TAG/SPANTX placeholders; "
                    "this is forbidden"
                )


def _save_epub_manifest(
    proj, source, extraction, chunk_count: int, record_count: int
) -> None:
    """Record EPUB v2 extraction metadata in manifest.json."""
    import json

    from booktx.config import write_manifest
    from booktx.models import EpubTemplateData, Manifest, ManifestSource

    template = EpubTemplateData(
        pipeline=EPUB_TEMPLATE_PIPELINE,
        epub2text_schema=EPUB2TEXT_SCHEMA,
        text2epub_manifest=extraction.text2epub_manifest,
        spans=extraction.span_refs,
        navigation=extraction.navigation,
    )
    manifest = Manifest(
        version=2,
        source=ManifestSource(
            filename=source.name,
            format="epub",
            source_language=proj.config.source_language,
            target_language=proj.config.target_language,
            sha256=extraction.source_sha256,
        ),
        chunk_count=chunk_count,
        record_count=record_count,
        template=template.model_dump(mode="json"),
    )
    write_manifest(proj, manifest)
    # names file convenience: keep names.json in sync if user edited it.
    _ = (json, NamesFile)  # touch imports for clarity


def _require_ready_context(proj, *, allow_missing_context: bool = False) -> bool:
    """Return True when context was checked and should be printed."""
    if allow_missing_context:
        return False
    ctx = load_context(proj)
    if ctx is None or not ctx.ready:
        _die("translation context is missing or not ready.\nRun: booktx context init .")
    return True


def _require_chunks(proj) -> list[Path]:
    chunk_paths = proj.chunks()
    if not chunk_paths:
        _die("No source chunks found. Run: booktx extract .")
    return chunk_paths


def _coverage_status(*, total: int, translated: int, has_error: bool) -> str:
    if has_error:
        return "invalid"
    if translated <= 0:
        return "pending"
    if translated >= total:
        return "complete"
    return "in_progress"


def _format_chunk_span(chunk_ids: list[str]) -> str:
    if not chunk_ids:
        return "-"
    if len(chunk_ids) == 1:
        return chunk_ids[0]
    return f"{chunk_ids[0]}..{chunk_ids[-1]}"


def _load_context_status(proj) -> tuple[bool, bool]:
    try:
        ctx = load_context(proj)
    except Exception as exc:  # noqa: BLE001
        _die(f"translation context is invalid: {exc}")
    return (ctx is not None, bool(ctx and ctx.ready))


def _chapter_map_for_workflow(proj):
    source_sha256 = project_source_sha256(proj)
    chapter_map = load_chapter_map(proj)
    if chapter_map is None or chapter_map.source_sha256 != source_sha256:
        chapter_map = detect_chapters(proj)
        write_chapter_map(proj, chapter_map)
    return chapter_map


def _project_status_snapshot(proj) -> dict[str, Any]:
    source_path = find_source_file(proj)
    manifest = load_manifest(proj)
    context_exists, context_ready = _load_context_status(proj)
    source_chunks = {chunk.chunk_id: chunk for chunk in load_source_chunks(proj)}
    source_records = load_source_records(proj)
    chapter_map = _chapter_map_for_workflow(proj)
    effective = load_effective_translated_chunks(proj, source_chunks=source_chunks)

    source_by_id = {record.record_id: record for record in source_records}
    translated_by_id = {
        record.id: record
        for chunk in effective.chunks.values()
        for record in chunk.records
    }
    findings = effective.findings
    record_error_by_id = {
        finding.record_id: finding
        for finding in findings
        if finding.severity == Severity.ERROR and finding.record_id
    }
    chunk_has_error = {
        finding.chunk_id
        for finding in findings
        if finding.severity == Severity.ERROR
        and finding.chunk_id not in {"context", "store"}
    }

    ordered_record_ids = [record.record_id for record in source_records]
    record_index_by_id = {
        record_id: idx for idx, record_id in enumerate(ordered_record_ids)
    }
    record_ids_by_chapter: dict[str, list[str]] = {}
    record_to_chapter: dict[str, str] = {}

    for chapter in chapter_map.chapters:
        start = record_index_by_id.get(chapter.start_record_id)
        end = record_index_by_id.get(chapter.end_record_id)
        if start is None or end is None or end < start:
            record_ids: list[str] = []
        else:
            record_ids = ordered_record_ids[start : end + 1]
        record_ids_by_chapter[chapter.chapter_id] = record_ids
        for record_id in record_ids:
            record_to_chapter[record_id] = chapter.chapter_id

    chunk_summaries: list[dict[str, Any]] = []
    for chunk in source_chunks.values():
        chunk_record_ids = [record.id for record in chunk.records]
        translated = [
            record_id for record_id in chunk_record_ids if record_id in translated_by_id
        ]
        source_words_total = sum(
            source_by_id[record_id].source_words for record_id in chunk_record_ids
        )
        source_words_translated = sum(
            source_by_id[record_id].source_words for record_id in translated
        )
        chunk_summaries.append(
            {
                "chunk_id": chunk.chunk_id,
                "records_total": len(chunk_record_ids),
                "records_translated": len(translated),
                "records_remaining": len(chunk_record_ids) - len(translated),
                "source_words_total": source_words_total,
                "source_words_translated": source_words_translated,
                "source_words_remaining": source_words_total - source_words_translated,
                "status": _coverage_status(
                    total=len(chunk_record_ids),
                    translated=len(translated),
                    has_error=chunk.chunk_id in chunk_has_error,
                ),
            }
        )

    chapter_summaries: list[dict[str, Any]] = []
    for chapter in chapter_map.chapters:
        chapter_record_ids = record_ids_by_chapter.get(chapter.chapter_id, [])
        translated = [
            record_id
            for record_id in chapter_record_ids
            if record_id in translated_by_id
        ]
        pending = [
            record_id
            for record_id in chapter_record_ids
            if record_id not in translated_by_id
        ]
        pending_chunk_ids: list[str] = []
        seen_pending_chunks: set[str] = set()
        for record_id in pending:
            chunk_id = source_by_id[record_id].chunk_id
            if chunk_id in seen_pending_chunks:
                continue
            seen_pending_chunks.add(chunk_id)
            pending_chunk_ids.append(chunk_id)
        source_words_total = sum(
            source_by_id[record_id].source_words for record_id in chapter_record_ids
        )
        source_words_translated = sum(
            source_by_id[record_id].source_words for record_id in translated
        )
        chapter_summaries.append(
            {
                "chapter_id": chapter.chapter_id,
                "title": chapter.title,
                "chunk_ids": list(chapter.chunk_ids),
                "pending_chunk_ids": pending_chunk_ids,
                "record_range": {
                    "start": chapter.start_record_id,
                    "end": chapter.end_record_id,
                },
                "records_total": len(chapter_record_ids),
                "records_translated": len(translated),
                "records_remaining": len(chapter_record_ids) - len(translated),
                "source_words_total": source_words_total,
                "source_words_translated": source_words_translated,
                "source_words_remaining": source_words_total - source_words_translated,
                "status": _coverage_status(
                    total=len(chapter_record_ids),
                    translated=len(translated),
                    has_error=any(
                        chunk_id in chunk_has_error for chunk_id in chapter.chunk_ids
                    ),
                ),
            }
        )

    chapters_by_id = {chapter["chapter_id"]: chapter for chapter in chapter_summaries}
    next_chapter = next(
        (chapter for chapter in chapter_summaries if chapter["records_remaining"] > 0),
        None,
    )

    total_source_words = sum(record.source_words for record in source_records)
    translated_source_words = sum(
        source_by_id[record_id].source_words for record_id in translated_by_id
    )
    chunks_complete = sum(
        1
        for chunk in chunk_summaries
        if chunk["records_translated"] == chunk["records_total"]
    )
    chunks_partial = sum(
        1
        for chunk in chunk_summaries
        if 0 < chunk["records_translated"] < chunk["records_total"]
    )
    chunks_pending = len(chunk_summaries) - chunks_complete - chunks_partial
    chapters_complete = sum(
        1
        for chapter in chapter_summaries
        if chapter["records_translated"] == chapter["records_total"]
    )
    chapters_partial = sum(
        1
        for chapter in chapter_summaries
        if 0 < chapter["records_translated"] < chapter["records_total"]
    )
    chapters_pending = len(chapter_summaries) - chapters_complete - chapters_partial

    selected_chapters: list[dict[str, Any]] = []
    source_sha256 = (
        manifest.source.sha256
        if manifest is not None and manifest.source.sha256
        else project_source_sha256(proj)
    )
    return {
        "version": 1,
        "project": str(proj.root),
        "source": {
            "filename": source_path.name,
            "format": proj.config.format,
            "source_language": proj.config.source_language,
            "target_language": proj.config.target_language,
            "source_sha256": source_sha256,
        },
        "context": {"exists": context_exists, "ready": context_ready},
        "totals": {
            "source_words": total_source_words,
            "translated_words": translated_source_words,
            "remaining_words": total_source_words - translated_source_words,
            "records_total": len(source_records),
            "records_translated": len(translated_by_id),
            "records_remaining": len(source_records) - len(translated_by_id),
            "chunks_total": len(chunk_summaries),
            "chunks_complete": chunks_complete,
            "chunks_partial": chunks_partial,
            "chunks_pending": chunks_pending,
            "chapters_total": len(chapter_summaries),
            "chapters_complete": chapters_complete,
            "chapters_partial": chapters_partial,
            "chapters_pending": chapters_pending,
            "invalid_translation_files": len(chunk_has_error),
            "stale_translation_files": len(
                {
                    finding.chunk_id
                    for finding in findings
                    if finding.rule == "stale_translation"
                }
            ),
        },
        "next": next_chapter,
        "chapters": selected_chapters,
        "_source_chunks": source_chunks,
        "_source_by_id": source_by_id,
        "_translated_by_id": translated_by_id,
        "_record_ids_by_chapter": record_ids_by_chapter,
        "_record_to_chapter": record_to_chapter,
        "_chapters_by_id": chapters_by_id,
        "_chunk_summaries": chunk_summaries,
        "_record_error_by_id": record_error_by_id,
    }


def _selected_chapter(
    summary: dict[str, Any], chapter_id: str | None
) -> dict[str, Any] | None:
    if chapter_id is None:
        return summary["next"]
    chapter = summary["_chapters_by_id"].get(chapter_id)
    if chapter is None:
        _die(f"unknown chapter id: {chapter_id}")
    return chapter


def _limit_records_by_words(
    record_ids: list[str], source_by_id: dict[str, Any], max_words: int
) -> list[str]:
    if max_words < 1:
        _die("--max-words must be >= 1")
    selected: list[str] = []
    total = 0
    for record_id in record_ids:
        words = source_by_id[record_id].source_words
        if selected and total + words > max_words:
            break
        selected.append(record_id)
        total += words
    return selected


def _select_translation_record_ids(
    summary: dict[str, Any],
    chapter: dict[str, Any],
    *,
    unit: str,
    max_words: int,
) -> tuple[str, list[str]]:
    source_by_id = summary["_source_by_id"]
    pending = [
        record_id
        for record_id in summary["_record_ids_by_chapter"][chapter["chapter_id"]]
        if record_id not in summary["_translated_by_id"]
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
            return (unit, _limit_records_by_words(same_span, source_by_id, max_words))
    return (unit, _limit_records_by_words(pending, source_by_id, max_words))


def _project_relative(path: Path, root: Path) -> str:
    """Return a stable project-relative display path when possible."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _write_ingest_template(proj, task: TranslationTask) -> Path:
    """Create the durable submission file for a task without overwriting work."""
    path = translation_ingest_path(proj, task.task_id)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "task_id": task.task_id,
        "records": [{"id": record.id, "target": ""} for record in task.records],
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _write_block_ingest_template(proj, task: TranslationTask) -> Path:
    """Create the durable block submission file for a task without overwriting work.

    The file starts with metadata comment headers (ignored by the block parser)
    followed by one `>>> RECORD_ID` header per record. The agent fills in the
    target text under each header.
    """
    path = translation_ingest_block_path(proj, task.task_id)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    source_display = _project_relative(
        translation_task_source_block_path(proj, task.task_id), proj.root
    )
    block_display = _project_relative(path, proj.root)
    submit_hint = (
        f"booktx translate insert . --task-id {task.task_id} "
        f"--file {block_display} --format block"
    )
    headers = [
        "# booktx block submission",
        f"# task: {task.task_id}",
        f"# source: {source_display}",
        f"# submit: {submit_hint}",
        "",
    ]
    parts = [f">>> {record.id}" for record in task.records]
    path.write_text(
        "\n".join(headers + parts).rstrip() + "\n",
        encoding="utf-8",
    )
    return path


def _write_task_source_block(proj, task: TranslationTask) -> Path:
    """Create the durable source-view file for a task without overwriting work.

    Holds the original source text for each record in the task so a coding
    agent can translate against a stable file instead of a large stdout dump.
    """
    path = translation_task_source_block_path(proj, task.task_id)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
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
    path.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    return path


def _make_task_id(chapter_id: str, first_record_id: str, record_ids: list[str]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    record_part = first_record_id.replace("-", "")
    digest = str(abs(hash("|".join(record_ids))))[-6:]
    return f"bt-task-{stamp}-{chapter_id}-{record_part}-{digest}"


def _create_translation_task(
    proj,
    summary: dict[str, Any],
    chapter: dict[str, Any],
    *,
    unit: str,
    record_ids: list[str],
) -> TranslationTask:
    source_by_id = summary["_source_by_id"]
    task = TranslationTask(
        task_id=_make_task_id(chapter["chapter_id"], record_ids[0], record_ids),
        unit=unit,  # type: ignore[arg-type]
        chapter_id=chapter["chapter_id"],
        chapter_title=chapter["title"],
        source_language=proj.config.source_language,
        target_language=proj.config.target_language,
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
    write_translation_task(proj, task)
    _write_ingest_template(proj, task)
    _write_block_ingest_template(proj, task)
    _write_task_source_block(proj, task)
    return task


def _print_status_human(
    summary: dict[str, Any], chapter: dict[str, Any] | None
) -> None:
    console.print(f"booktx status — {summary['project']}")
    console.print()
    console.print(f"Source: {summary['source']['filename']}")
    console.print(f"Source language: {summary['source']['source_language']}")
    console.print(f"Target language: {summary['source']['target_language']}")
    console.print(f"Context: {'READY' if summary['context']['ready'] else 'NOT READY'}")
    console.print()
    totals = summary["totals"]
    console.print(f"Total source words: {totals['source_words']:>10,}")
    console.print(f"Translated words:   {totals['translated_words']:>10,}")
    console.print(f"Remaining words:    {totals['remaining_words']:>10,}")
    console.print()
    console.print(
        f"Chunks:   {totals['chunks_complete']} / {totals['chunks_total']} complete, "
        f"{totals['chunks_partial']} partial, {totals['chunks_pending']} pending"
    )
    console.print(
        f"Chapters: {totals['chapters_complete']} / "
        f"{totals['chapters_total']} complete, "
        f"{totals['chapters_partial']} partial, {totals['chapters_pending']} pending"
    )
    if totals["invalid_translation_files"] or totals["stale_translation_files"]:
        console.print(
            f"Translation files: {totals['invalid_translation_files']} invalid, "
            f"{totals['stale_translation_files']} stale"
        )
    ready_for_final = (
        totals["records_remaining"] == 0 and totals["invalid_translation_files"] == 0
    )
    console.print()
    console.print(f"Ready for final build: {'yes' if ready_for_final else 'no'}")
    if not ready_for_final:
        if totals["remaining_words"] > 0:
            console.print(
                f"Reason: {totals['remaining_words']:,} source words remain "
                "untranslated"
            )
        elif totals["invalid_translation_files"] > 0:
            console.print(
                "Reason: "
                f"{totals['invalid_translation_files']} translation file(s) "
                "are invalid"
            )
    detail = chapter or summary["next"]
    if detail is None:
        return
    console.print()
    console.print("Next chapter:" if chapter is None else "Chapter:")
    console.print(f"  {detail['chapter_id']}  {detail['title']}".rstrip())
    console.print(f"  status: {detail['status']}")
    console.print(
        f"  records: {detail['records_translated']} / "
        f"{detail['records_total']} translated, "
        f"{detail['records_remaining']} remaining"
    )
    console.print(
        f"  words: {detail['source_words_translated']:,} / "
        f"{detail['source_words_total']:,} translated, "
        f"{detail['source_words_remaining']:,} remaining"
    )
    console.print(f"  chunks: {_format_chunk_span(detail['chunk_ids'])}")
    console.print(
        f"  pending chunks: {_format_chunk_span(detail['pending_chunk_ids'])}"
    )
    console.print(
        "  record range: "
        f"{detail['record_range']['start']}..{detail['record_range']['end']}"
    )


def _print_translate_task(
    task: TranslationTask,
    proj,
    *,
    as_json: bool,
    output_format: str,
    show_sources: bool = False,
    show_template: bool = False,
) -> None:
    ingest_path = translation_ingest_path(proj, task.task_id)
    ingest_display = _project_relative(ingest_path, proj.root)
    block_ingest_path = translation_ingest_block_path(proj, task.task_id)
    block_ingest_display = _project_relative(block_ingest_path, proj.root)
    source_block_path = translation_task_source_block_path(proj, task.task_id)
    source_block_display = _project_relative(source_block_path, proj.root)
    json_submit_hint = (
        f"booktx translate insert . --task-id {task.task_id} "
        f"--json-file {ingest_display}"
    )
    block_submit_hint = (
        f"booktx translate insert . --task-id {task.task_id} "
        f"--file {block_ingest_display} --format block"
    )
    block_stdin_submit_hint = (
        f"booktx translate insert . --task-id {task.task_id} "
        "--stdin --format block <<'BOOKTX'"
    )
    view_sources_hint = f"cat {source_block_display}"
    payload = {
        "version": 1,
        "task_id": task.task_id,
        "unit": task.unit,
        "chapter_id": task.chapter_id,
        "chapter_title": task.chapter_title,
        "source_language": task.source_language,
        "target_language": task.target_language,
        "source_words": task.source_words,
        "record_count": task.record_count,
        "records": [record.model_dump(mode="json") for record in task.records],
        "ingest_path": ingest_display,
        "block_ingest_path": block_ingest_display,
        "source_block_path": source_block_display,
        "submit_hint": json_submit_hint,
        "block_submit_hint": block_submit_hint,
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    if output_format == "tsv":
        console.print(f"# task: {task.task_id}")
        console.print(f"# chapter: {task.chapter_id}\t{task.chapter_title}".rstrip())
        for record in task.records:
            console.print(f"{record.id}\t{record.source}")
        console.print(f"# write translation JSON to: {ingest_display}")
        console.print(f"# submit: {json_submit_hint}")
        return
    if output_format == "block":
        console.print(f"task: {task.task_id}")
        console.print(f"chapter: {task.chapter_id}  {task.chapter_title}".rstrip())
        console.print(f"unit: {task.unit}")
        console.print(f"records: {task.record_count}")
        console.print(f"source words: {task.source_words}")
        console.print()
        console.print(f"Source file: {source_block_display}", soft_wrap=True)
        console.print(f"Durable block template: {block_ingest_display}", soft_wrap=True)
        console.print(f"Submit durable file with: {block_submit_hint}", soft_wrap=True)
        console.print(f"View sources: {view_sources_hint}", soft_wrap=True)
        if show_template:
            console.print()
            console.print("Heredoc template (optional, for tiny manual fixes):")
            console.print()
            console.print(block_stdin_submit_hint, soft_wrap=True)
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
    console.print(ingest_display)
    console.print("Submit with:")
    console.print(json_submit_hint)


def _load_translation_task_or_exit(proj, task_id: str) -> TranslationTask:
    task = load_translation_task(proj, task_id)
    if task is None:
        _die(f"unknown task id: {task_id} ({translation_task_path(proj, task_id)})")
    return task


def _parse_json_submission(text: str) -> tuple[str | None, list[dict[str, str]]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        _die(f"invalid JSON submission: {exc.msg} (line {exc.lineno} col {exc.colno})")
    if not isinstance(payload, dict):
        _die("JSON submission must be an object")
    records = payload.get("records")
    if not isinstance(records, list):
        _die("JSON submission must contain a 'records' array")
    parsed: list[dict[str, str]] = []
    for item in records:
        if not isinstance(item, dict):
            _die("each submitted record must be an object")
        record_id = str(item.get("id", "")).strip()
        target = item.get("target")
        if not record_id or not isinstance(target, str):
            _die("each submitted record must contain string fields 'id' and 'target'")
        parsed.append({"id": record_id, "target": target})
    task_id = payload.get("task_id")
    return (str(task_id).strip() if task_id else None, parsed)


def _parse_tsv_submission(text: str) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        if "\t" not in line:
            _die(f"malformed TSV line {line_no}: expected '<record-id><TAB><target>'")
        record_id, target = line.split("\t", 1)
        if not record_id.strip():
            _die(f"malformed TSV line {line_no}: missing record id")
        parsed.append({"id": record_id.strip(), "target": target})
    return parsed


_BLOCK_HEADER_RE = re.compile(r"^>>>\s+(?P<id>\S+)\s*$")


def _trim_blank_edge_lines(lines: list[str]) -> str:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def _parse_block_submission(text: str) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    current_id: str | None = None
    current_lines: list[str] = []
    seen: set[str] = set()

    def flush() -> None:
        nonlocal current_id, current_lines
        if current_id is None:
            return
        # Strip trailing separator lines (blank or comment) that sit between
        # this record and the next header (or EOF). Internal and leading
        # comment lines are preserved as target text.
        lines = list(current_lines)
        while lines and (
            not lines[-1].strip() or lines[-1].lstrip().startswith("#")
        ):
            lines.pop()
        target = _trim_blank_edge_lines(lines)
        if not target:
            _die(f"empty target for record {current_id}")
        parsed.append({"id": current_id, "target": target})
        current_id = None
        current_lines = []

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        header = _BLOCK_HEADER_RE.match(raw_line)
        if header:
            flush()
            record_id = header.group("id").strip()
            if record_id in seen:
                _die(f"duplicate record id in block submission: {record_id}")
            seen.add(record_id)
            current_id = record_id
            current_lines = []
            continue
        if current_id is None:
            stripped = raw_line.strip()
            if stripped and not stripped.startswith("#"):
                _die(
                    f"malformed block submission line {line_no}: "
                    "expected '>>> <record-id>' before target text"
                )
            continue
        current_lines.append(raw_line)

    flush()
    if not parsed:
        _die("block submission did not contain any records")
    return parsed


def _render_submission_failures(findings) -> None:
    console.print("[red]error:[/red] submission rejected; no files changed")
    console.print()
    for finding in findings:
        if finding.record_id:
            console.print(f"{finding.record_id} {finding.rule}:")
        else:
            console.print(f"{finding.chunk_id} {finding.rule}:")
        console.print(f"  {finding.message}")


def _next_chapter(proj, *, print_context: bool) -> None:
    summary = _project_status_snapshot(proj)
    chapter = summary["next"]
    if chapter is None:
        console.print("All chapter records have accepted translations.")
        raise typer.Exit(code=1)
    if print_context:
        console.print(f"context: {context_markdown_path(proj)}", soft_wrap=True)
    console.print(f"chapter: {chapter['chapter_id']}  {chapter['title']}".rstrip())
    console.print(f"status: {chapter['status']}")
    console.print(
        "record range: "
        f"{chapter['record_range']['start']}..{chapter['record_range']['end']}"
    )
    console.print(
        f"records: {chapter['records_translated']} / "
        f"{chapter['records_total']} translated, "
        f"{chapter['records_remaining']} remaining"
    )
    console.print(f"chunks: {_format_chunk_span(chapter['chunk_ids'])}")
    console.print(f"pending chunks: {_format_chunk_span(chapter['pending_chunk_ids'])}")
    console.print(f"source words remaining: {chapter['source_words_remaining']:,}")
    console.print(
        "[dim]next command:[/dim] "
        f"booktx translate next . --chapter {chapter['chapter_id']} --unit batch "
        "--max-words 500 --format block"
    )
    raise typer.Exit(code=0)


# --- next --------------------------------------------------------------------


@app.command(name="status")
def status_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    chapter: str | None = typer.Option(
        None, "--chapter", help="Optional chapter id to focus the report."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit stable JSON output."),
) -> None:
    """Report record-aware translation progress."""
    try:
        proj = load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    _require_chunks(proj)
    summary = _project_status_snapshot(proj)
    selected = _selected_chapter(summary, chapter)
    if selected is not None:
        summary["chapters"] = [selected]
        summary["next"] = selected
    if as_json:
        payload = {
            "version": summary["version"],
            "project": summary["project"],
            "source": summary["source"],
            "context": summary["context"],
            "totals": summary["totals"],
            "next": summary["next"],
            "chapters": summary["chapters"],
        }
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    _print_status_human(summary, selected)


@app.command(name="next")
def next_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    allow_missing_context: bool = typer.Option(
        False,
        "--allow-missing-context",
        help="Legacy override: allow next without a ready translation context.",
    ),
    unit: str = typer.Option(
        "chunk", "--unit", help="Translation unit to return: chunk or chapter."
    ),
) -> None:
    """Print the next pending legacy work item and point callers at translate/*."""
    try:
        proj = load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return

    if unit not in {"chunk", "chapter"}:
        _die("--unit must be chunk or chapter")
    _require_chunks(proj)
    print_context = _require_ready_context(
        proj, allow_missing_context=allow_missing_context
    )
    if unit == "chapter":
        _next_chapter(proj, print_context=print_context)
        return
    summary = _project_status_snapshot(proj)
    pending_chunks = [
        chunk["chunk_id"]
        for chunk in summary["_chunk_summaries"]
        if chunk["records_remaining"] > 0
    ]
    if not pending_chunks:
        console.print("All chunk records have accepted translations.")
        raise typer.Exit(code=1)
    if print_context:
        console.print(f"context: {context_markdown_path(proj)}", soft_wrap=True)
    cid = pending_chunks[0]
    chunk_path = proj.chunks_dir / f"{cid}.json"
    records_remaining = next(
        chunk["records_remaining"]
        for chunk in summary["_chunk_summaries"]
        if chunk["chunk_id"] == cid
    )
    console.print(f"{cid}\t{chunk_path}", soft_wrap=True)
    console.print(f"records remaining: {records_remaining}")
    console.print("[dim]submit with:[/dim]")
    console.print(f"booktx translate next {project_dir} --unit chunk", soft_wrap=True)
    console.print("booktx translate insert . --stdin")
    raise typer.Exit(code=0)


# --- chapters ---------------------------------------------------------------


@app.command(name="chapters")
def chapters_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
) -> None:
    """Detect and list chapter ranges."""
    try:
        proj = load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    chapter_map = detect_chapters(proj)
    write_chapter_map(proj, chapter_map)
    for chapter in chapter_map.chapters:
        chunks = ", ".join(chapter.chunk_ids)
        title = f"  {chapter.title}" if chapter.title else ""
        console.print(
            f"{chapter.chapter_id}{title}\tchunks: {chunks}\t"
            f"records: {chapter.start_record_id}..{chapter.end_record_id}"
        )


@app.command(name="next-chapter")
def next_chapter_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    allow_missing_context: bool = typer.Option(
        False,
        "--allow-missing-context",
        help="Legacy override: allow next-chapter without ready context.",
    ),
) -> None:
    """Print the next incomplete chapter and all chunks it covers."""
    try:
        proj = load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    _require_chunks(proj)
    print_context = _require_ready_context(
        proj, allow_missing_context=allow_missing_context
    )
    _next_chapter(proj, print_context=print_context)


@translate_app.command(name="next")
def translate_next(
    project_dir: Path = typer.Argument(..., help="Project directory."),
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
) -> None:
    """Return the next text to translate and persist a task id."""
    try:
        proj = load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    if unit not in {"paragraph", "batch", "chunk", "chapter"}:
        _die("--unit must be paragraph, batch, chunk, or chapter")
    if output_format not in {"text", "tsv", "block"}:
        _die("--format must be text, tsv, or block")
    if as_json and output_format != "text":
        _die("--json cannot be combined with --format")
    _require_chunks(proj)
    _require_ready_context(proj, allow_missing_context=allow_missing_context)
    summary = _project_status_snapshot(proj)
    selected_chapter = _selected_chapter(summary, chapter)
    if selected_chapter is None:
        console.print("All records already have accepted translations.")
        raise typer.Exit(code=1)
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
        unit=actual_unit,
        record_ids=record_ids,
    )
    _print_translate_task(
        task,
        proj,
        as_json=as_json,
        output_format=output_format,
        show_sources=show_sources,
        show_template=show_template,
    )


@translate_app.command(name="insert")
def translate_insert(
    project_dir: Path = typer.Argument(..., help="Project directory."),
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
) -> None:
    """Accept translated text through the CLI and write the store atomically."""
    try:
        proj = load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    if input_format not in {"json", "tsv", "block"}:
        _die("--format must be json, tsv, or block")
    _require_chunks(proj)
    _require_ready_context(proj, allow_missing_context=allow_missing_context)

    submitted: list[dict[str, str]] = []
    payload_task_id: str | None = None
    if record_id is not None or target is not None:
        if not record_id or target is None:
            _die("--record-id and --target must be supplied together")
        submitted = [{"id": record_id, "target": target}]
    elif json_file is not None:
        payload_task_id, submitted = _parse_json_submission(
            _read_submission_file_or_die(json_file)
        )
    elif input_file is not None:
        raw = _read_submission_file_or_die(input_file)
        if input_format == "json":
            payload_task_id, submitted = _parse_json_submission(raw)
        elif input_format == "tsv":
            submitted = _parse_tsv_submission(raw)
        else:
            submitted = _parse_block_submission(raw)
    elif stdin:
        raw = sys.stdin.read()
        if input_format == "json":
            payload_task_id, submitted = _parse_json_submission(raw)
        elif input_format == "tsv":
            submitted = _parse_tsv_submission(raw)
        else:
            submitted = _parse_block_submission(raw)
    else:
        _die("provide one of --record-id/--target, --json-file, --file, or --stdin")

    effective_task_id = task_id or payload_task_id
    task = (
        _load_translation_task_or_exit(proj, effective_task_id)
        if effective_task_id
        else None
    )
    allowed_ids = {record.id for record in task.records} if task is not None else None
    summary = _project_status_snapshot(proj)
    source_by_id = summary["_source_by_id"]
    source_chunks = summary["_source_chunks"]
    failures = []

    seen_ids: set[str] = set()
    for item in submitted:
        record_id = item["id"]
        if record_id in seen_ids:
            _die(f"duplicate record id in submission: {record_id}")
        seen_ids.add(record_id)
        if record_id not in source_by_id:
            _die(f"unknown source record id: {record_id}")
        if allowed_ids is not None and record_id not in allowed_ids:
            _die(f"record {record_id} is not part of task {task.task_id}")
        source_view = source_by_id[record_id]
        translated = TranslatedRecord(id=record_id, target=item["target"])
        source_chunk = source_chunks[source_view.chunk_id]
        source_record = next(
            record for record in source_chunk.records if record.id == record_id
        )
        failures.extend(
            validate_record_pair(
                source_record, translated, source_chunk.chunk_id, load_context(proj)
            )
        )

    if any(finding.severity == Severity.ERROR for finding in failures):
        _render_submission_failures(
            [finding for finding in failures if finding.severity == Severity.ERROR]
        )
        raise typer.Exit(code=1)

    store = load_translation_store(proj)
    store.source_sha256 = summary["source"]["source_sha256"]
    updated_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    for item in submitted:
        source_view = source_by_id[item["id"]]
        store.records[item["id"]] = StoredTranslationRecord(
            chunk_id=source_view.chunk_id,
            source_sha256=source_view.source_sha256,
            target=item["target"],
            updated_at=updated_at,
        )
    write_translation_store(proj, store)

    refreshed = _project_status_snapshot(proj)
    first_record_id = submitted[0]["id"]
    chapter_id = refreshed["_record_to_chapter"].get(first_record_id, "")
    chapter = refreshed["_chapters_by_id"].get(chapter_id)
    target_words = sum(count_words(item["target"]) for item in submitted)
    console.print(
        f"accepted: {len(submitted)} record(s), {target_words} target word(s)"
    )
    if chapter is not None:
        console.print(f"chapter: {chapter['chapter_id']} {chapter['title']}".rstrip())
        console.print(
            f"progress: {chapter['records_translated']} / "
            f"{chapter['records_total']} records translated, "
            f"{chapter['records_remaining']} remaining"
        )
        console.print(
            "next: "
            f"booktx translate next . --chapter {chapter['chapter_id']} --unit batch "
            "--max-words 500 --format block"
        )


@translate_app.command(name="import-legacy")
def translate_import_legacy(
    project_dir: Path = typer.Argument(..., help="Project directory."),
) -> None:
    """Import valid legacy translated chunk files into the translation store."""
    try:
        proj = load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    _require_chunks(proj)
    store = load_translation_store(proj)
    imported_records = 0
    imported_chunks = 0
    source_chunks = {chunk.chunk_id: chunk for chunk in load_source_chunks(proj)}
    for chunk_id, source_chunk in source_chunks.items():
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
            if record.id in store.records:
                continue
            source_record = source_records[record.id]
            store.records[record.id] = StoredTranslationRecord(
                chunk_id=chunk_id,
                source_sha256=source_record_sha256(source_record.source),
                target=record.target,
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


@translate_app.command(name="export")
def translate_export(
    project_dir: Path = typer.Argument(..., help="Project directory."),
) -> None:
    """Export fully accepted store-backed chunks into translated/*.json."""
    try:
        proj = load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    _require_chunks(proj)
    store = load_translation_store(proj)
    exported = 0
    for chunk in load_source_chunks(proj):
        if not all(record.id in store.records for record in chunk.records):
            continue
        translated_chunk = TranslatedChunk(
            chunk_id=chunk.chunk_id,
            records=[
                TranslatedRecord(id=record.id, target=store.records[record.id].target)
                for record in chunk.records
            ],
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
        (proj.translated_dir / f"{chunk.chunk_id}.json").write_text(
            translated_chunk.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        exported += 1
    console.print(f"exported: {exported} chunk(s) to {proj.translated_dir}")


@translate_app.command(name="task-status")
def translate_task_status(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    task_id: str = typer.Option(..., "--task-id", help="Task id to inspect."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Report accepted vs missing progress for one persisted translation task.

    Makes interrupted translation runs diagnosable without inspecting the store
    by hand. Exits 0 only when every task record is accepted and current.
    """
    try:
        proj = load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
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
        accepted_ids.append(record.id)

    total = len(task.records)
    accepted = len(accepted_ids)
    not_current = total - accepted
    first_missing = (missing_ids[0] if missing_ids
                     else (stale_ids[0] if stale_ids else None))
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
    submit_hint = (
        f"booktx translate insert . --task-id {task.task_id} "
        f"--file {block_ingest_display} --format block"
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
    console.print(
        f"records: {accepted} / {total} accepted, {not_current} missing"
    )
    if stale_ids:
        console.print(f"stale: {len(stale_ids)} record(s) need re-translation")
    if first_missing is not None:
        console.print(f"first missing: {first_missing}")
    console.print(f"source file: {source_display}", soft_wrap=True)
    console.print(f"ingest file: {block_ingest_display}", soft_wrap=True)
    console.print(f"submit: {submit_hint}", soft_wrap=True)
    raise typer.Exit(code=0 if complete else 1)


@translate_app.command(name="set-record")
def translate_set_record(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    task_id: str = typer.Option(..., "--task-id", help="Task id owning the record."),
    record_id: str = typer.Option(..., "--record-id", help="Record id to set."),
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
    """Commit a single translated record from stdin (or --target).

    Lets an agent safely commit one record at a time so work already written to
    translation-store.json survives interruption. Prefer this over embedding a
    whole chapter section in one shell command when truncation is a concern.
    """
    try:
        proj = load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
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

    if not target_text.strip():
        _die(f"empty target for record {record_id}")

    summary = _project_status_snapshot(proj)
    source_by_id = summary["_source_by_id"]
    source_chunks = summary["_source_chunks"]
    if record_id not in source_by_id:
        _die(f"unknown source record id: {record_id}")
    source_view = source_by_id[record_id]
    source_chunk = source_chunks[source_view.chunk_id]
    source_record = next(
        record for record in source_chunk.records if record.id == record_id
    )
    translated = TranslatedRecord(id=record_id, target=target_text)
    failures = validate_record_pair(
        source_record, translated, source_chunk.chunk_id, load_context(proj)
    )
    if any(finding.severity == Severity.ERROR for finding in failures):
        _render_submission_failures(
            [finding for finding in failures if finding.severity == Severity.ERROR]
        )
        raise typer.Exit(code=1)

    store = load_translation_store(proj)
    store.source_sha256 = summary["source"]["source_sha256"]
    updated_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    store.records[record_id] = StoredTranslationRecord(
        chunk_id=source_view.chunk_id,
        source_sha256=source_view.source_sha256,
        target=target_text,
        updated_at=updated_at,
    )
    write_translation_store(proj, store)

    refreshed = _project_status_snapshot(proj)
    chapter_id = refreshed["_record_to_chapter"].get(record_id, "")
    chapter = refreshed["_chapters_by_id"].get(chapter_id)
    target_words = count_words(target_text)
    console.print(
        f"accepted: 1 record, {target_words} target word(s)"
    )
    if chapter is not None:
        console.print(f"chapter: {chapter['chapter_id']} {chapter['title']}".rstrip())
        console.print(
            f"progress: {chapter['records_translated']} / "
            f"{chapter['records_total']} records translated, "
            f"{chapter['records_remaining']} remaining"
        )


# --- validate ----------------------------------------------------------------


@app.command()
def validate(
    project_dir: Path = typer.Argument(..., help="Project directory."),
) -> None:
    """Validate translated chunks against the translation contract."""
    try:
        proj = load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return

    report = validate_project(proj)
    out = write_report(proj, report)

    if report.findings:
        for f in report.findings:
            color = "red" if f.severity == "error" else "yellow"
            loc = f" [{f.record_id}]" if f.record_id else ""
            console.print(
                f"[{color}]{f.severity}[/{color}] {f.chunk_id}{loc} "
                f"{f.rule}: {f.message}"
            )
    console.print(
        f"chunks_checked={report.chunks_checked} "
        f"passed={report.chunks_passed} "
        f"errors={len(report.errors)} warnings={len(report.warnings)} "
        f"missing={report.chunks_missing_translation}"
    )
    console.print(f"[dim]report:[/dim] {out}")
    if not report.passed:
        raise typer.Exit(code=1)


# --- build -------------------------------------------------------------------


@app.command()
def build(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    require_complete: bool = typer.Option(
        False,
        "--require-complete",
        help="Fail when any record is untranslated or invalid.",
    ),
) -> None:
    """Rebuild the translated document into ``output/``."""
    try:
        proj = load_project(project_dir)
        result = build_project(proj, require_complete=require_complete)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    except BuildError as exc:
        _die(str(exc))
        return

    console.print(f"[green]Built[/green] {result.format} -> {result.output_path}")
    if result.report:
        changed_entries = result.report.get("changed_entries", [])
        console.print(
            "  changed_entries="
            f"{_changed_entry_count(changed_entries)} "
            f"replacements={result.report.get('replacement_count', 0)} "
            f"unresolved_tokens={result.report.get('unresolved_token_count', 0)}"
        )


def _changed_entry_count(changed_entries) -> object:
    if isinstance(changed_entries, list):
        return len(changed_entries)
    return changed_entries


# --- top-level callback (version) --------------------------------------------


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Print booktx version and exit.",
        is_eager=True,
    ),
) -> None:
    """booktx root options."""
    if version:
        console.print(__version__)
        raise typer.Exit


def main() -> None:
    """Console-script entry point (used by pyproject [project.scripts])."""
    # Typer raises typer.Exit for normal command exits; surface its code.
    try:
        app()
    except typer.Exit as exc:
        sys.exit(exc.exit_code)


__all__ = ["app", "main"]
