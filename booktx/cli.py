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

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from booktx import __version__
from booktx.build import BuildError, build_project
from booktx.chapters import detect_chapters, write_chapter_map
from booktx.chunking import spans_to_chunks
from booktx.config import (
    BooktxError,
    find_source_file,
    init_project,
    load_project,
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
from booktx.models import NamesFile
from booktx.validate import validate_project, write_report

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
app.add_typer(context_app, name="context")


def _die(message: str, code: int = 1) -> None:
    """Print an error and exit with ``code``."""
    console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(code=code)


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
    console.print(f"context: {context_markdown_path(proj)}")


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


def _next_chapter(proj, *, print_context: bool) -> None:
    chapter_map = detect_chapters(proj)
    write_chapter_map(proj, chapter_map)
    translated_ids = set(proj.translated_ids())
    for chapter in chapter_map.chapters:
        pending = [cid for cid in chapter.chunk_ids if cid not in translated_ids]
        if not pending:
            continue
        if print_context:
            console.print(f"context: {context_markdown_path(proj)}")
        title = f"  {chapter.title}" if chapter.title else ""
        console.print(f"chapter: {chapter.chapter_id}{title}")
        console.print("chunks:")
        for cid in chapter.chunk_ids:
            console.print(f"  {proj.chunks_dir / f'{cid}.json'}")
        console.print(f"[dim]write translations to:[/dim] {proj.translated_dir}/*.json")
        raise typer.Exit(code=0)
    console.print("All chapter chunks have translations.")
    raise typer.Exit(code=1)


# --- next --------------------------------------------------------------------


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
    """Print the first untranslated chunk and exit 0, or exit 1 when done.

    No files are written (no skeleton). Exit codes:
      0 — a chunk is ready to translate (its id + path printed).
      1 — context is missing/not ready, or every chunk is already translated.
    """
    try:
        proj = load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return

    if unit not in {"chunk", "chapter"}:
        _die("--unit must be chunk or chapter")
    chunk_paths = _require_chunks(proj)
    print_context = _require_ready_context(
        proj, allow_missing_context=allow_missing_context
    )
    if unit == "chapter":
        _next_chapter(proj, print_context=print_context)
        return
    if print_context:
        console.print(f"context: {context_markdown_path(proj)}")
    chunk_ids = {path.stem for path in chunk_paths}
    translated_ids = set(proj.translated_ids())
    pending = sorted(cid for cid in chunk_ids if cid not in translated_ids)
    if not pending:
        console.print(
            f"All {len(chunk_ids)} chunk(s) have translations in {proj.translated_dir}."
        )
        raise typer.Exit(code=1)

    cid = pending[0]
    chunk_path = proj.chunks_dir / f"{cid}.json"
    out_path = proj.translated_dir / f"{cid}.json"
    console.print(f"{cid}\t{chunk_path}")
    console.print(f"[dim]write translation to:[/dim] {out_path}")
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
) -> None:
    """Rebuild the translated document into ``output/``."""
    try:
        proj = load_project(project_dir)
        result = build_project(proj)
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
