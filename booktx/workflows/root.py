# ruff: noqa: B008,E501
"""Root Typer commands and workflows extracted from cli.py (Phase 3 slice 8)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer
from rich.table import Table

from booktx.build import BuildError, build_project
from booktx.chapters import (
    ChapterMap,
    detect_chapters,
    load_chapter_map,
    write_chapter_map,
)
from booktx.chunking import RECORD_ID_SCHEME, segmenter_metadata, spans_to_chunks
from booktx.cli_support import (
    _block_on_epub_audit_errors,
    _changed_entry_count,
    _die,
    _epub_output_audit_findings,
    _handle_booktx_error,
    _load_runtime_or_exit,
    _next_chapter,
    _print_identity,
    _print_status_human,
    _project_status_snapshot,
    _reject_if_isolated,
    _render_profiles_overview_human,
    _render_validate_findings,
    _require_chunks,
    _require_ready_context,
    _selected_chapter,
    console,
)
from booktx.config import (
    BooktxError,
    Project,
    find_source_file,
    init_project,
    load_manifest,
    load_project,
    load_source_project,
    load_translation_store,
    project_source_sha256,
    protected_terms_sha256,
    translation_store_path,
)
from booktx.context import context_markdown_path
from booktx.epub_io import EpubExtraction, extract_epub
from booktx.epub_manifest import EPUB2TEXT_SCHEMA, EPUB_TEMPLATE_PIPELINE
from booktx.markdown_io import extract_markdown
from booktx.models import Chunk, Manifest, NamesFile, TranslatedChunk
from booktx.pass_through import ensure_pass_through_profile, run_pass_through
from booktx.path_display import display_path
from booktx.status import build_profiles_overview
from booktx.validate import (
    Severity,
    validate_project,
    validation_exits_nonzero,
    write_report,
)

root_app = typer.Typer()
doctor_app = typer.Typer(help="Diagnostic commands.")

# --- init --------------------------------------------------------------------


@root_app.command()
def init(
    project_dir: Path = typer.Argument(..., help="Directory to create the project in."),
    target: str | None = typer.Option(
        None, "--target", "-t", help="Optional target language code, e.g. de."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Optional profile name to create when --target is used."
    ),
    source_lang: str = typer.Option(
        "en",
        "--source",
        "--source-lang",
        "-s",
        help="Source language code (default: en).",
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
            target_language=target or "",
            profile_name=profile,
            source_language=source_lang,
            source_file=source,
            chunk_size=chunk_size,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return

    if target:
        console.print(f"[green]Initialized source project:[/green] {proj.root}")
        console.print(f"[green]Created profile:[/green] {proj.profile}")
        console.print(f"[green]Selected active profile:[/green] {proj.profile}")
    else:
        console.print(f"[green]Initialized source project:[/green] {proj.root}")
    console.print(f"  source_language: {proj.config.source_language}")
    if proj.config.target_language:
        console.print(f"  target_language: {proj.config.target_language}")
    console.print(f"  format:          {proj.config.format}")
    if proj.config.source_file:
        console.print(f"  source_file:     {proj.config.source_file}")
    else:
        console.print(
            "  [yellow]source/ is empty — drop a .md or .epub file into it.[/yellow]"
        )


# --- inspect -----------------------------------------------------------------


@root_app.command()
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


def _load_names_list(proj: Project) -> list[str]:
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
        entries_raw = extraction.text2epub_manifest.get("entries", [])
        entries = entries_raw if isinstance(entries_raw, list) else []
        block_entries = [
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("blocks")
        ]
        details = f"{len(block_entries)} spine document(s) with text blocks"
    else:  # pragma: no cover - config validation already guards this
        raise BooktxError("unsupported_format", f"Unsupported format {fmt!r}")

    from booktx.chunking import segment_spans

    records = segment_spans(spans, language=source_language)
    return len(records), details


def _chunk_json_texts(chunks: list[Chunk] | list[TranslatedChunk]) -> dict[str, str]:
    return {
        f"{chunk.chunk_id}.json": chunk.model_dump_json(indent=2) + "\n"
        for chunk in chunks
    }


def _has_accepted_store_records(proj: Project) -> bool:
    path = translation_store_path(proj)
    if not path.is_file():
        return False
    store = load_translation_store(proj)
    return any(
        any(candidate.status == "accepted" for candidate in record.versions)
        for record in store.records.values()
    )


def _same_extract_settings(
    manifest: Manifest,
    *,
    chunk_size: int,
    source_language: str,
    names_sha256: str,
) -> bool:
    return (
        manifest.chunk_size == chunk_size
        and manifest.record_id_scheme == RECORD_ID_SCHEME
        and manifest.segmenter == segmenter_metadata(source_language)
        and manifest.names_sha256 == names_sha256
    )


def _guard_extract_repeatability_and_rechunk(
    proj: Project,
    *,
    current_source_sha256: str,
    chunk_texts: dict[str, str],
    names_sha256: str,
    force_rechunk: bool,
) -> str | None:
    previous_manifest = load_manifest(proj)
    if previous_manifest is None:
        return None

    previous_source_sha256 = previous_manifest.source.sha256
    same_source = bool(previous_source_sha256) and (
        previous_source_sha256 == current_source_sha256
    )

    if (
        same_source
        and previous_manifest.record_id_scheme == RECORD_ID_SCHEME
        and previous_manifest.chunk_size != proj.config.chunk_size
        and _has_accepted_store_records(proj)
        and not force_rechunk
    ):
        _die(
            "chunk_size changed from "
            f"{previous_manifest.chunk_size} to {proj.config.chunk_size}, but this "
            f"project uses record_id_scheme={RECORD_ID_SCHEME}.\n"
            "Changing chunk_size would renumber record ids and orphan existing "
            "translation-store entries.\n"
            "Use the existing chunk_size, or run `booktx extract --force-rechunk` "
            "after backing up or migrating translations."
        )

    if same_source and _same_extract_settings(
        previous_manifest,
        chunk_size=proj.config.chunk_size,
        source_language=proj.config.source_language,
        names_sha256=names_sha256,
    ):
        existing_chunks = {
            path.name: path.read_text("utf-8")
            for path in sorted(proj.chunks(), key=lambda path: path.name)
        }
        if existing_chunks and existing_chunks != chunk_texts:
            _die(
                "repeatability violated: the same source and extraction settings "
                "did not reproduce byte-identical chunk files; refusing to replace "
                "the existing chunks."
            )

    if previous_source_sha256 and previous_source_sha256 != current_source_sha256:
        return (
            "source file changed since the previous extraction; validation may "
            "report stale translations."
        )
    return None


# --- extract -----------------------------------------------------------------


@root_app.command()
def extract(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    force_rechunk: bool = typer.Option(
        False,
        "--force-rechunk",
        help="Allow a risky chunk-size rechunk when chunk-local ids would be renumbered.",
    ),
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

    # Idempotent rebuild of chunks/ — write into a sibling temp dir and swap
    # it in atomically so an interrupted extract never leaves a half-empty
    # .booktx/chunks/.
    import tempfile

    from booktx.epub_manifest import sha256_path as _sha256
    from booktx.io_utils import write_text_atomic

    current_source_sha256 = (
        extraction.source_sha256 if fmt == "epub" else _sha256(source)
    )
    names_sha256 = protected_terms_sha256(names)
    chunk_texts = _chunk_json_texts(chunks)
    warning_message = _guard_extract_repeatability_and_rechunk(
        proj,
        current_source_sha256=current_source_sha256,
        chunk_texts=chunk_texts,
        names_sha256=names_sha256,
        force_rechunk=force_rechunk,
    )

    proj.booktx_dir.mkdir(parents=True, exist_ok=True)
    tmp_chunks = Path(tempfile.mkdtemp(prefix=".chunks.", dir=proj.booktx_dir))
    try:
        for filename, text in chunk_texts.items():
            write_text_atomic(tmp_chunks / filename, text)
        # Remove the previous chunks dir and move the temp one into place.
        if proj.chunks_dir.exists():
            shutil.rmtree(proj.chunks_dir)
        tmp_chunks.replace(proj.chunks_dir)
    except BaseException:
        shutil.rmtree(tmp_chunks, ignore_errors=True)
        raise

    record_count = sum(len(c.records) for c in chunks)
    epub_audit_warning = ""
    if fmt == "epub":
        _save_epub_manifest(proj, source, extraction, len(chunks), record_count)
        epub_audit_warning = _write_epub_chapter_map_and_audit(proj)
    elif fmt == "markdown":
        from booktx.config import write_manifest
        from booktx.models import Manifest, ManifestSource

        write_manifest(
            proj,
            Manifest(
                version=1,
                source=ManifestSource(
                    filename=source.name,
                    format="markdown",
                    source_language=proj.config.source_language,
                    target_language=proj.config.target_language,
                    sha256=current_source_sha256,
                ),
                chunk_count=len(chunks),
                record_count=record_count,
                chunk_size=proj.config.chunk_size,
                record_id_scheme=RECORD_ID_SCHEME,
                segmenter=segmenter_metadata(proj.config.source_language),
                names_sha256=names_sha256,
            ),
        )
    console.print(
        f"[green]Extracted[/green] {len(chunks)} chunk(s), "
        f"{record_count} record(s) into {proj.chunks_dir}"
    )
    if warning_message:
        console.print(f"[yellow]warning:[/yellow] {warning_message}", soft_wrap=True)
    if epub_audit_warning:
        console.print(f"[yellow]warning:[/yellow] {epub_audit_warning}", soft_wrap=True)
        console.print("[dim]details: booktx chapters . --audit[/dim]", soft_wrap=True)


def _assert_epub_records_are_clean(chunks: list[Chunk]) -> None:
    for chunk in chunks:
        for record in chunk.records:
            if "__TAG_" in record.source or "__SPANTX_" in record.source:
                raise BooktxError(
                    "epub_placeholders_leaked",
                    "new EPUB extraction produced TAG/SPANTX placeholders; "
                    "this is forbidden",
                )


def _save_epub_manifest(
    proj: Project,
    source: Path,
    extraction: EpubExtraction,
    chunk_count: int,
    record_count: int,
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
        chapter_mapping="epub2text-block-v1",
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
        chunk_size=proj.config.chunk_size,
        record_id_scheme=RECORD_ID_SCHEME,
        segmenter=segmenter_metadata(proj.config.source_language),
        names_sha256=protected_terms_sha256(_load_names_list(proj)),
        template=template.model_dump(mode="json"),
    )
    write_manifest(proj, manifest)
    # names file convenience: keep names.json in sync if user edited it.
    _ = (json, NamesFile)  # touch imports for clarity


def _write_epub_chapter_map_and_audit(proj: Project) -> str:
    """Detect and persist the chapter map and audit after EPUB extraction.

    Returns a one-line warning string when the audit has findings, or "". The
    extraction itself stays successful: this is a completeness signal, not a
    policy gate, so preview/truncated EPUBs with warning-only findings still
    extract cleanly.
    """
    from booktx.epub_toc_audit import audit_epub_chapter_map, write_audit_report

    chapter_map = detect_chapters(proj)
    write_chapter_map(proj, chapter_map)
    result = audit_epub_chapter_map(proj, chapter_map=chapter_map)
    write_audit_report(proj, result)
    if not result.findings:
        return ""
    bits: list[str] = []
    if result.error_findings:
        bits.append(f"{len(result.error_findings)} error(s)")
    if result.warning_findings:
        bits.append(f"{len(result.warning_findings)} warning(s)")
    return (
        "EPUB chapter audit: "
        + ", ".join(bits)
        + " (visible TOC vs extracted chapters)."
    )


def _chapter_map_for_workflow(proj: Project) -> ChapterMap:
    """Refresh-and-load helper retained for direct callers outside status.py."""
    source_sha256 = project_source_sha256(proj)
    chapter_map = load_chapter_map(proj)
    if chapter_map is None or chapter_map.source_sha256 != source_sha256:
        chapter_map = detect_chapters(proj)
        write_chapter_map(proj, chapter_map)
    return chapter_map


# --- next --------------------------------------------------------------------


@root_app.command(name="status")
def status_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    chapter: str | None = typer.Option(
        None, "--chapter", help="Optional chapter id to focus the report."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit stable JSON output."),
) -> None:
    """Report record-aware translation progress."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=False)
    proj = runtime.project
    if proj.layout_version == "profiles" and proj.profile is None:
        overview = build_profiles_overview(load_source_project(proj.root))
        if as_json:
            console.print_json(
                json.dumps(overview.model_dump(mode="json"), ensure_ascii=False)
            )
            return
        _render_profiles_overview_human(overview)
        return
    _require_chunks(proj)
    summary = _project_status_snapshot(proj)
    selected = _selected_chapter(summary, chapter)
    if selected is not None:
        summary.snapshot.chapters = [selected]
        summary.snapshot.next = selected
    if runtime.mode.isolated_output:
        summary.snapshot.project = display_path(
            proj.profile_dir or proj.root, runtime.mode
        )
    if as_json:
        console.print_json(
            json.dumps(summary.snapshot.model_dump(mode="json"), ensure_ascii=False)
        )
        return
    _print_status_human(summary, selected)


@root_app.command(name="next")
def next_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
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
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project

    if unit not in {"chunk", "chapter"}:
        _die("--unit must be chunk or chapter")
    _require_chunks(proj)
    print_context = _require_ready_context(
        proj, allow_missing_context=allow_missing_context
    )
    if unit == "chapter":
        _next_chapter(proj, print_context=print_context, mode=runtime.mode)
        return
    if runtime.mode.isolated_output:
        _die(
            "booktx next is not available in profile-root isolated mode; use `booktx translate next .` instead"
        )
    summary = _project_status_snapshot(proj)
    _block_on_epub_audit_errors(summary)
    pending_chunks = [
        chunk.chunk_id
        for chunk in summary.index.chunk_summaries
        if chunk.records_remaining > 0
    ]
    if not pending_chunks:
        console.print("All chunk records have accepted translations.")
        raise typer.Exit(code=1)
    if print_context:
        console.print(f"context: {context_markdown_path(proj)}", soft_wrap=True)
    cid = pending_chunks[0]
    chunk_path = proj.chunks_dir / f"{cid}.json"
    records_remaining = next(
        chunk.records_remaining
        for chunk in summary.index.chunk_summaries
        if chunk.chunk_id == cid
    )
    console.print(f"{cid}\t{chunk_path}", soft_wrap=True)
    console.print(f"records remaining: {records_remaining}")
    console.print("[dim]submit with:[/dim]")
    profile_part = f" --profile {proj.profile}" if proj.profile else ""
    console.print(
        f"booktx translate next {project_dir}{profile_part} --unit chunk",
        soft_wrap=True,
    )
    console.print(f"booktx translate insert .{profile_part} --stdin")
    raise typer.Exit(code=0)


# --- chapters ---------------------------------------------------------------


@root_app.command(name="chapters")
def chapters_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    audit: bool = typer.Option(
        False,
        "--audit",
        help=(
            "Audit the EPUB visible TOC against extracted spans, navigation, "
            "and the chapter map; writes .booktx/reports/chapter-audit.json."
        ),
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON output.",
    ),
) -> None:
    """Detect and list chapter ranges, or audit EPUB chapter completeness."""
    try:
        proj = load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    if audit:
        _run_chapter_audit(proj, as_json=as_json)
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


def _run_chapter_audit(proj: Project, *, as_json: bool = False) -> None:
    from booktx.epub_toc_audit import (
        audit_epub_chapter_map,
        write_audit_report,
    )

    if proj.config.format != "epub":
        if as_json:
            console.print_json('{"error": "chapter audit is EPUB-only"}')
        else:
            _die("chapter audit is EPUB-only")
        return
    chapter_map = load_chapter_map(proj)
    if chapter_map is None:
        # Read-only audit: detect without persisting so chapter-map.json is
        # not mutated by --audit.
        chapter_map = detect_chapters(proj)
    result = audit_epub_chapter_map(proj, chapter_map=chapter_map)
    out_path = write_audit_report(proj, result)
    if as_json:
        console.print_json(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        return
    console.print("EPUB chapter audit")
    console.print(f"toc entries: {len(result.toc_entries)}")
    console.print(f"numbered TOC chapters: {result.numbered_toc_count}")
    console.print(f"numbered chapters in map: {result.mapped_numbered_chapter_count}")
    console.print(f"extracted documents: {result.extracted_document_count}")
    if result.missing_numbered_titles:
        preview = ", ".join(result.missing_numbered_titles[:12])
        suffix = "" if len(result.missing_numbered_titles) <= 12 else ", ..."
        console.print(f"missing numbered chapters: {preview}{suffix}")
    if not result.findings:
        console.print("findings: none")
    else:
        for finding in result.findings:
            severity_color = {
                "error": "red",
                "warning": "yellow",
                "info": "cyan",
            }.get(finding.severity, "white")
            console.print(
                f"[{severity_color}]{finding.severity}[/{severity_color}] "
                f"{finding.code}: {finding.message}",
                soft_wrap=True,
            )
    console.print(f"report: {out_path}")


@root_app.command(name="next-chapter")
def next_chapter_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    allow_missing_context: bool = typer.Option(
        False,
        "--allow-missing-context",
        help="Legacy override: allow next-chapter without ready context.",
    ),
) -> None:
    """Print the next incomplete chapter and all chunks it covers."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    _require_chunks(proj)
    print_context = _require_ready_context(
        proj, allow_missing_context=allow_missing_context
    )
    _next_chapter(proj, print_context=print_context, mode=runtime.mode)


# --- validate ----------------------------------------------------------------


@root_app.command()
def validate(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    include_inactive: bool = typer.Option(
        False,
        "--include-inactive",
        help="Also validate inactive historical translation versions.",
    ),
    fail_on_history_warnings: bool = typer.Option(
        False,
        "--fail-on-history-warnings",
        help=(
            "Imply --include-inactive and exit non-zero on inactive-version warnings."
        ),
    ),
    all_versions_strict: bool = typer.Option(
        False,
        "--all-versions-strict",
        help="Imply --include-inactive and keep inactive-version errors fatal.",
    ),
    chapter: str | None = typer.Option(
        None, "--chapter", help="Scope to a specific chapter id."
    ),
    task_id: str | None = typer.Option(
        None, "--task-id", help="Scope to a specific task id."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
    fail_on_warnings: bool = typer.Option(
        False,
        "--fail-on-warnings",
        help="Exit non-zero when validation reports warnings.",
    ),
) -> None:
    """Validate translated chunks against the translation contract."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project

    report = validate_project(
        proj,
        include_inactive_versions=(
            include_inactive or fail_on_history_warnings or all_versions_strict
        ),
        all_versions_strict=all_versions_strict,
        chapter_id=chapter,
        task_id=task_id,
    )
    out = write_report(proj, report)

    if as_json:
        console.print_json(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        _render_validate_findings(report)
        console.print(
            f"chunks_checked={report.chunks_checked} "
            f"passed={report.chunks_passed} "
            f"errors={len(report.errors)} warnings={len(report.warnings)} "
            f"missing={report.chunks_missing_translation}"
        )
        console.print("[dim]report:[/dim] ", end="")
        console.print(display_path(out, runtime.mode), soft_wrap=True, markup=False)
    if validation_exits_nonzero(
        report,
        fail_on_warnings=fail_on_warnings,
        fail_on_history_warnings=fail_on_history_warnings,
    ):
        raise typer.Exit(code=1)
        raise typer.Exit(code=1)


@root_app.command()
def check(
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
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
    fail_on_warnings: bool = typer.Option(
        True,
        "--fail-on-warnings/--no-fail-on-warnings",
        help="Exit non-zero when validation reports warnings.",
    ),
    epub_output: bool = typer.Option(
        False,
        "--epub-output",
        help="Audit the existing expected EPUB output for policy compliance without building.",
    ),
) -> None:
    """Scoped build-preflight check for inline XHTML and translation contracts.

    A human-friendly alias for scoped validation + EPUB inline-XHTML preflight.
    Prefer this after each chapter translation and before build.

    ``--epub-output`` audits the expected EPUB output path produced by a prior
    build against the resolved EPUB output policy. It does not build or modify
    the EPUB and emits the same findings in text and JSON modes.
    """
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project

    if epub_output:
        audit_findings, audit_payload = _epub_output_audit_findings(proj)
        if as_json:
            console.print_json(json.dumps(audit_payload, indent=2, ensure_ascii=False))
        else:
            _render_validate_findings(type("R", (), {"findings": audit_findings})())
            console.print(
                f"errors={sum(1 for f in audit_findings if f.severity == Severity.ERROR)} "
                f"warnings={sum(1 for f in audit_findings if f.severity == Severity.WARN)}"
            )
        has_blocking = any(f.severity == Severity.ERROR for f in audit_findings) or (
            fail_on_warnings
            and any(f.severity == Severity.WARN for f in audit_findings)
        )
        if has_blocking:
            raise typer.Exit(code=1)
        return

    report = validate_project(proj, chapter_id=chapter, task_id=task_id)

    if as_json:
        console.print_json(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        _render_validate_findings(report)
        console.print(
            f"chunks_checked={report.chunks_checked} "
            f"passed={report.chunks_passed} "
            f"errors={len(report.errors)} warnings={len(report.warnings)} "
            f"missing={report.chunks_missing_translation}"
        )
    if validation_exits_nonzero(report, fail_on_warnings=fail_on_warnings):
        raise typer.Exit(code=1)


# --- build -------------------------------------------------------------------


@root_app.command()
def build(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    require_complete: bool = typer.Option(
        False,
        "--require-complete",
        help="Fail when any record is untranslated or invalid.",
    ),
    require_reviewed: bool = typer.Option(
        False,
        "--require-reviewed",
        help="Fail when required review coverage is missing or stale.",
    ),
) -> None:
    """Rebuild the translated document into ``output/``."""
    try:
        runtime = _load_runtime_or_exit(
            project_dir, profile=profile, require_profile=True
        )
        proj = runtime.project
        result = build_project(
            proj,
            require_complete=require_complete,
            require_reviewed=require_reviewed,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    except BuildError as exc:
        _die(str(exc))
        return

    console.print(
        f"[green]Built[/green] {result.format} -> "
        f"{display_path(result.output_path, runtime.mode)}"
    )
    if result.report:
        changed_entries = result.report.get("changed_entries", [])
        console.print(
            "  changed_entries="
            f"{_changed_entry_count(changed_entries)} "
            f"replacements={result.report.get('replacement_count', 0)} "
            f"unresolved_tokens={result.report.get('unresolved_token_count', 0)}"
        )


@root_app.command(name="pass-through")
def pass_through_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str = typer.Option(..., "--profile", help="Pass-through profile name."),
    create: bool = typer.Option(
        False, "--create", help="Create the pass-through profile if missing."
    ),
    select: bool = typer.Option(False, "--select", help="Select the created profile."),
    output_filename: str | None = typer.Option(
        None, "--output-filename", help="Output filename for a newly created profile."
    ),
    force: bool = typer.Option(
        True, "--force/--no-force", help="Refresh existing generated translated chunks."
    ),
    prune_stale: bool = typer.Option(
        True,
        "--prune-stale/--keep-stale",
        help="Remove stale generated translated chunks.",
    ),
    clear_store: bool = typer.Option(
        False,
        "--clear-store",
        help="Clear store records that would override generated chunks.",
    ),
    no_build: bool = typer.Option(
        False, "--no-build", help="Only generate and validate translated chunks."
    ),
    allow_warnings: bool = typer.Option(
        False, "--allow-warnings", help="Do not fail on validation warnings."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Generate identity translated chunks, validate coverage, and rebuild output.

    This produces a source-as-target reconstruction fixture. It is not a real
    translation: each record target equals its source text. Compare the output
    against the source with a diff viewer to detect reconstruction drift.
    """
    runtime = _load_runtime_or_exit(project_dir, require_profile=False)
    _reject_if_isolated(runtime)
    try:
        proj = ensure_pass_through_profile(
            runtime.project.root,
            profile,
            create=create,
            select=select,
            output_filename=output_filename,
        )
        result = run_pass_through(
            proj,
            force=force,
            prune_stale=prune_stale,
            clear_store=clear_store,
            build=not no_build,
            allow_warnings=allow_warnings,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    except BuildError as exc:
        _die(str(exc))
        return

    payload = {
        "profile": result.profile,
        "chunks_written": result.chunks_written,
        "records_written": result.records_written,
        "stale_removed": result.stale_removed,
        "translated_dir": str(result.translated_dir),
        "validation": {
            "errors": len(result.validation_report.errors),
            "warnings": len(result.validation_report.warnings),
            "missing": result.validation_report.chunks_missing_translation,
        },
        "output_path": str(result.build_result.output_path)
        if result.build_result
        else None,
        "format": result.build_result.format if result.build_result else None,
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    console.print(f"pass-through profile: {result.profile}")
    console.print(f"translated chunks: {result.chunks_written}")
    console.print(f"translated records: {result.records_written}")
    console.print(f"removed stale translated chunks: {result.stale_removed}")
    console.print(
        "validation: passed "
        f"errors={len(result.validation_report.errors)} "
        f"warnings={len(result.validation_report.warnings)} "
        f"missing={result.validation_report.chunks_missing_translation}"
    )
    if result.build_result is not None:
        console.print(
            f"[green]Built[/green] {result.build_result.format} -> "
            f"{result.build_result.output_path}"
        )


# --- top-level callback (version) --------------------------------------------


# --- qa scan command -----------------------------------------------------


@root_app.command(name="qa-scan")
def qa_scan_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    target_only: bool = typer.Option(
        False, "--target-only", help="Search targets only, omit source."
    ),
    forbidden: bool = typer.Option(
        False, "--forbidden", help="Check for forbidden glossary terms in targets."
    ),
    glossary: bool = typer.Option(
        False, "--glossary", help="Report required glossary target mismatches."
    ),
    include_advisory: bool = typer.Option(
        False, "--include-advisory", help="Include non-binding approved glossary suggestions."
    ),
    target_contains: str | None = typer.Option(
        None,
        "--target-contains",
        help="Literal substring to find in effective targets.",
    ),
    pattern: str | None = typer.Option(
        None, "--pattern", help="Regex pattern to match in targets."
    ),
    language_leftovers: str | None = typer.Option(
        None, "--language-leftovers", help="Detect source-language leftovers (e.g. en)."
    ),
    chapter: str | None = typer.Option(
        None, "--chapter", help="Scope to one chapter id."
    ),
    jsonl: bool = typer.Option(
        False, "--jsonl", help="Output one JSON object per finding per line."
    ),
) -> None:
    """Scan effective targets for QA findings without scripting."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project

    from booktx.qa_scan import qa_scan

    bundle = _project_status_snapshot(proj)

    try:
        result = qa_scan(
            proj,
            bundle,
            chapter_id=chapter,
            target_only=target_only,
            forbidden=forbidden,
            glossary=glossary,
            target_contains=target_contains,
            pattern=pattern,
            language_leftovers=language_leftovers,
            include_advisory=include_advisory,
        )
    except ValueError as exc:
        _die(str(exc))
        return

    if jsonl:
        import json as _json

        for finding in result.findings:
            console.print(
                _json.dumps(finding.as_dict(), ensure_ascii=False),
                soft_wrap=True,
                markup=False,
            )
    else:
        console.print(
            f"scanned {result.records_scanned} records, "
            f"{result.findings_count} findings"
        )
        for finding in result.findings:
            console.print(
                f"  {finding.id} [{finding.rule}/{finding.severity}] {finding.term}"
                f" -> {finding.target[:80]}..."
                if len(finding.target) > 80
                else f"  {finding.id} [{finding.rule}/{finding.severity}] {finding.term} -> {finding.target}",
                soft_wrap=True,
                markup=False,
            )


# --- translation search command -------------------------------------------


# --- root + doctor commands (extracted to commands/root.py in slice 8) -------


@root_app.command(name="whoami")
def whoami(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show resolved translation identity and project status."""
    _print_identity(project_dir, profile=profile, as_json=as_json)


@root_app.command(name="mode")
def mode_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory or profile root."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show how booktx resolved the current working path."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=False)
    payload = {
        "mode": runtime.mode.kind,
        "profile": runtime.mode.profile_name,
        "profiles_visible": not runtime.mode.isolated_output,
        "cross_profile_access": not runtime.mode.isolated_output,
        "safe_for_model_evaluation": runtime.mode.isolated_output,
        "source_access": runtime.mode.source_access,
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    console.print(f"mode: {payload['mode']}")
    if payload["profile"]:
        console.print(f"profile: {payload['profile']}")
    console.print(f"profiles visible: {'yes' if payload['profiles_visible'] else 'no'}")
    console.print(
        f"cross-profile access: {'yes' if payload['cross_profile_access'] else 'no'}"
    )
    console.print(
        "safe for model evaluation: "
        f"{'yes' if payload['safe_for_model_evaluation'] else 'no'}"
    )


@doctor_app.command(name="isolation")
def doctor_isolation_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory or profile root."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Check whether the current path is ready for isolated evaluation."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=False)
    proj = runtime.project
    marker_exists = bool(
        runtime.mode.profile_root
        and (runtime.mode.profile_root / ".booktx-profile.json").is_file()
    )
    profile_local_context = bool(
        proj.context_json_path is not None
        and runtime.mode.profile_root is not None
        and proj.context_json_path.parent == runtime.mode.profile_root
    )
    profile_local_store = bool(
        proj.store_path is not None
        and runtime.mode.profile_root is not None
        and proj.store_path.parent == runtime.mode.profile_root
    )
    profile_local_ledger = bool(
        proj.ledger_path is not None
        and runtime.mode.profile_root is not None
        and proj.ledger_path.parent == runtime.mode.profile_root
    )
    redacted_samples = [
        display_path(proj.root, runtime.mode),
        display_path(proj.chunks_dir, runtime.mode),
        display_path(proj.profile_dir or proj.root, runtime.mode),
    ]
    path_redaction_pass = all(
        not sample.startswith("/")
        and "../" not in sample
        and (runtime.mode.profile_name or "") not in sample.replace(".", "")
        for sample in redacted_samples[:2]
    )
    source_available = bool(proj.chunks())
    passed = (
        runtime.mode.isolated_output
        and marker_exists
        and source_available
        and profile_local_context
        and profile_local_store
        and profile_local_ledger
        and path_redaction_pass
    )
    payload = {
        "isolation": "PASS" if passed else "FAIL",
        "mode": runtime.mode.kind,
        "profile": runtime.mode.profile_name,
        "source_broker": "available" if source_available else "unavailable",
        "cross_profile_commands": "blocked"
        if runtime.mode.isolated_output
        else "available",
        "path_redaction": "PASS" if path_redaction_pass else "FAIL",
        "source_access": runtime.mode.source_access,
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
    else:
        console.print(f"isolation: {payload['isolation']}")
        console.print(f"mode: {payload['mode']}")
        if payload["profile"]:
            console.print(f"profile: {payload['profile']}")
        console.print(f"source broker: {payload['source_broker']}")
        console.print(f"cross-profile commands: {payload['cross_profile_commands']}")
        console.print(f"path redaction: {payload['path_redaction']}")
    if not passed:
        raise typer.Exit(code=1)


__all__ = ["root_app", "doctor_app"]
